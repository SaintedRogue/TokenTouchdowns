/**
 * Pure state core for the live draft room (docs/draft-room-design.md
 * sections 4.1/4.2). No network, no subprocess, no file IO, no timers --
 * this module is handed data and returns state, so everything hard about
 * draft day (Yahoo lagging, sessions expiring, the poller re-delivering the
 * same pick five times) is a problem for the caller, not for this file.
 *
 * Every function here is immutable: state in, new state out, original
 * untouched. That is what makes the whole thing trivially testable and what
 * lets the HTTP layer treat state as a plain value it can snapshot, diff, or
 * roll back.
 */

// --- snake order -------------------------------------------------------------

/**
 * Every pick number belonging to `slot` (1-based) across `rounds` rounds of
 * a `teams`-team snake draft (docs/draft-room-design.md 4.1).
 *
 * Per round r (0-based): r even -> r*teams + slot; r odd -> r*teams +
 * (teams - slot + 1). The two formulas are mirror images of each other,
 * which is exactly what makes the round boundary wrap correctly: the slot
 * that picks LAST in an even round (slot = teams) picks FIRST in the next
 * (odd) round, and the slot that picks FIRST in an odd round's neighbour
 * (slot = 1) picks LAST in it -- so slot 1 and slot `teams` each take two
 * picks back-to-back, once per full pair of rounds, at opposite ends of the
 * board. That double-pick is not a bug to guard against; it is the point of
 * a snake draft.
 */
export function myPicks(teams, slot, rounds) {
  const picks = [];
  for (let r = 0; r < rounds; r++) {
    const pick = r % 2 === 0 ? r * teams + slot : r * teams + (teams - slot + 1);
    picks.push(pick);
  }
  return picks;
}

/**
 * The smallest of my picks that is >= currentPick, or null once every one
 * of my picks is already behind the current pick (my draft is over).
 * `myPicks` is strictly increasing round over round, so a linear scan finds
 * it directly -- no need to re-derive the round/slot math here.
 */
export function nextPick(teams, slot, currentPick, rounds) {
  for (const pick of myPicks(teams, slot, rounds)) {
    if (pick >= currentPick) return pick;
  }
  return null;
}

// --- parseDraftResults: the risky, shape-unverified surface -----------------
//
// league/{key}/draftresults is confirmed reachable, but the account behind
// this repo has never completed a draft, so the shape of one entry is
// UNVERIFIED until draft day (docs/draft-room-design.md 4.2). src/normalize.js
// exists precisely because Yahoo's v2 JSON is irregular -- collections can
// arrive as a count-keyed numeric-key object, as a bare array of
// single-key-wrapped items, or spliced into the parent under numeric keys
// alongside real attributes. A draft_results collection with exactly one
// pick is the sharpest edge of that irregularity: normalize()'s own
// collection-unwrap logic only fires for a `count`-bearing object or a
// repeated-key array of 2+ items, so a single pick can legitimately reach
// this function either bare (`{pick, team_key, player_key}`) or still
// sitting under a wrapper key (`{draft_result: {...}}`).
//
// The response to all of that is the same: never guess wrong, never throw,
// never drop. An entry that cannot be resolved to all three of
// (pick, teamKey, playerKey) is returned in `malformed`, not silently
// skipped -- that is what lets the caller raise the loud banner design
// section 4.2 and section 5 both require, instead of quietly drafting on a
// board that no longer matches Yahoo.

const isPlainObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

/**
 * A raw (pre-normalize.js) Yahoo collection fragment: numeric-string keys
 * alongside a `count` sibling. Handled here too, defensively, in case a
 * caller ever forwards a draft_results fragment straight from Yahoo without
 * running it through normalize() first -- the exact shape normalize.js's
 * own `collectionToArray` exists to handle.
 */
function isRawCollection(v) {
  if (!isPlainObject(v)) return false;
  const keys = Object.keys(v);
  return keys.includes('count') && keys.some((k) => /^\d+$/.test(k));
}

function rawCollectionToArray(v) {
  return Object.keys(v)
    .filter((k) => /^\d+$/.test(k))
    .sort((a, b) => Number(a) - Number(b))
    .map((k) => v[k]);
}

/**
 * Reduce whatever `payload` turns out to be into an array of "raw entry"
 * candidates, without ever throwing. A bare primitive (string/number/
 * boolean) is not a recognisable draft-results shape at all, but it must
 * not vanish either -- it is wrapped as a single candidate so it surfaces
 * in `malformed` rather than being silently discarded.
 */
function toEntryArray(payload) {
  if (payload == null) return [];
  if (Array.isArray(payload)) return payload;
  if (isRawCollection(payload)) return rawCollectionToArray(payload);
  return [payload];
}

/**
 * Peel back single-key wrapper objects (`{draft_result: {...}}`,
 * potentially nested) until something that is not a single-key-object
 * wrapper is reached. Bounded to a handful of levels -- Yahoo's nesting is
 * shallow, and an unbounded walk is a needless way to hang on adversarial
 * input. A well-formed entry always has 3+ keys (pick, team_key,
 * player_key), so it can never be mistaken for a wrapper here; this only
 * ever fires on entries that would otherwise be misclassified as malformed.
 */
function unwrapSingleKeyObject(v) {
  let cur = v;
  for (let i = 0; i < 4 && isPlainObject(cur); i++) {
    const keys = Object.keys(cur);
    if (keys.length !== 1) break;
    const inner = cur[keys[0]];
    if (!isPlainObject(inner)) break;
    cur = inner;
  }
  return cur;
}

function asPositiveInt(v) {
  if (typeof v === 'number') return Number.isInteger(v) && v > 0 ? v : null;
  if (typeof v === 'string' && /^\d+$/.test(v.trim())) {
    const n = Number(v.trim());
    return n > 0 ? n : null;
  }
  return null;
}

function asNonEmptyString(v) {
  return typeof v === 'string' && v.trim() !== '' ? v : null;
}

/**
 * Pull {pick, teamKey, playerKey} out of one unwrapped entry. Field names
 * are unverified (see module docstring above), so a couple of plausible
 * spellings are accepted alongside Yahoo's documented `pick` / `team_key` /
 * `player_key` convention. Returns null -- never throws -- when any of the
 * three required fields cannot be found or coerced.
 */
function extractEntry(entry) {
  const pick = asPositiveInt(entry.pick ?? entry.pick_number ?? entry.pickNumber);
  const teamKey = asNonEmptyString(entry.team_key ?? entry.teamKey);
  const playerKey = asNonEmptyString(entry.player_key ?? entry.playerKey);
  if (pick === null || teamKey === null || playerKey === null) return null;
  return { pick, teamKey, playerKey };
}

/**
 * Normalise a draft_results payload into `{picks, malformed}`. Accepts an
 * array of entries, a single entry not wrapped in a list, an entry still
 * sitting under a wrapper key, or the raw count-keyed collection shape.
 * Never throws: garbage in must not crash a draft-day server, so anything
 * that cannot be resolved to a full pick is reported in `malformed` rather
 * than thrown or dropped.
 */
export function parseDraftResults(payload) {
  const picks = [];
  const malformed = [];
  for (const raw of toEntryArray(payload)) {
    const unwrapped = unwrapSingleKeyObject(raw);
    const parsed = isPlainObject(unwrapped) ? extractEntry(unwrapped) : null;
    if (parsed) picks.push(parsed);
    else malformed.push(raw);
  }
  return { picks, malformed };
}

// --- state -------------------------------------------------------------------

/**
 * A fresh draft-room state: every player in `playerIds` available, nothing
 * drafted yet, current pick 1.
 */
export function createState(playerIds) {
  return {
    available: new Set(playerIds ?? []),
    drafted: new Map(),
    myRoster: [],
    currentPick: 1,
    // Player ids removed via markTaken, most recent last. Tracked
    // separately from `drafted` (which is Yahoo-authoritative only) so
    // `undo` can never reach a real Yahoo pick -- see `undo` below.
    manualMarks: [],
    myTeamKey: null,
  };
}

/**
 * Fold Yahoo's parsed draft_results picks into state. Pure and idempotent:
 * the poller re-delivers picks it has already seen every few seconds for
 * the length of a two-hour draft, so re-applying an already-known pick
 * (same pick number, same player, same team) must be a complete no-op.
 * `available`, `drafted`, and `myRoster` are recomputed from the merged
 * pick set on every call rather than incrementally appended to -- that is
 * what makes redelivery, and redelivery in any order, safe: there is no
 * "did I already push this to myRoster" bookkeeping to get wrong, because
 * myRoster is always freshly derived from `drafted`.
 *
 * `currentPick` is `drafted.size + 1` -- the COUNT of distinct picks
 * recorded -- rather than `(max pick number seen) + 1`. Yahoo may deliver
 * picks out of order (a slow poll catching two picks in one response, or a
 * retry racing a newer request), and a max-based counter would report the
 * wrong "on the clock" pick if a lower-numbered pick becomes known after a
 * higher-numbered one already is.
 *
 * Reconciliation: a Yahoo pick for a player already manually marked (see
 * markTaken) drops that player's manual mark instead of layering a second
 * "taken" record on top of it. Yahoo is authoritative, so after this the
 * player is recorded exactly once, in `drafted`, and is no longer reachable
 * by `undo` -- exactly as a real pick should be.
 */
export function applyPicks(state, picks, { myTeamKey } = {}) {
  const drafted = new Map(state.drafted);
  const manualMarks = [...state.manualMarks];
  const available = new Set(state.available);
  const resolvedMyTeamKey = myTeamKey ?? state.myTeamKey ?? null;

  for (const p of picks ?? []) {
    if (!p || typeof p.pick !== 'number' || !p.playerKey || !p.teamKey) continue;

    const existing = drafted.get(p.pick);
    if (existing && existing.playerId === p.playerKey && existing.teamKey === p.teamKey) {
      continue; // already applied -- idempotent no-op
    }

    const manualIdx = manualMarks.indexOf(p.playerKey);
    if (manualIdx !== -1) manualMarks.splice(manualIdx, 1);

    available.delete(p.playerKey);
    drafted.set(p.pick, { playerId: p.playerKey, teamKey: p.teamKey, pick: p.pick });
  }

  const currentPick = drafted.size + 1;
  const myRoster = [...drafted.values()]
    .filter((d) => resolvedMyTeamKey && d.teamKey === resolvedMyTeamKey)
    .sort((a, b) => a.pick - b.pick)
    .map((d) => d.playerId);

  return {
    ...state, available, drafted, manualMarks, currentPick, myRoster, myTeamKey: resolvedMyTeamKey,
  };
}

/**
 * Manual override (design section 5): the human marks a player taken
 * outside Yahoo's own feed -- Yahoo lagging, or drafting off-platform
 * entirely. Idempotent: marking an already-unavailable player (whether
 * already manually marked or already a real Yahoo pick) is a no-op that
 * returns the same state reference unchanged.
 */
export function markTaken(state, playerId) {
  if (!state.available.has(playerId)) return state;
  const available = new Set(state.available);
  available.delete(playerId);
  const manualMarks = [...state.manualMarks, playerId];
  return { ...state, available, manualMarks };
}

/**
 * Revert the most recent MANUAL mark only. Yahoo is authoritative: undoing
 * a real pick would desync the board from a draft that has already
 * happened on Yahoo's side. This only ever pops `manualMarks` -- a player
 * reconciled into `drafted` by applyPicks has already been removed from
 * `manualMarks` (see there) and is therefore unreachable here. With no
 * manual marks pending, this is a no-op.
 */
export function undo(state) {
  if (state.manualMarks.length === 0) return state;
  const manualMarks = [...state.manualMarks];
  const playerId = manualMarks.pop();
  const available = new Set(state.available);
  available.add(playerId);
  return { ...state, available, manualMarks };
}
