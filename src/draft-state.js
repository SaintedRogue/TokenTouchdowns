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

// --- parseDraftResults: pick / pending / malformed ---------------------------
//
// Verified live against a real 14-team, 15-round Yahoo mock draft
// (docs/draft-room-design.md 4.2): `draft_results` is published in full from
// the very first poll -- one entry per pick slot for the WHOLE draft (210 of
// them for 14 teams x 15 rounds), every one carrying `pick`, `round`, and
// `team_key`. Only the slots that have actually happened also carry a
// `player_key`; every future slot is present too, just without one:
//
//   made:    {"pick":1,"round":1,"team_key":"470.l.10417423.t.1","player_key":"470.p.40059"}
//   pending: {"pick":2,"round":1,"team_key":"470.l.10417423.t.2"}          <- no player_key
//
// A pending slot is a NORMAL, EXPECTED state -- not a malformation. Treating
// it as malformed (as this function used to) means the very first poll of a
// real draft reports 1 pick and 209 malformed entries, which raises the loud
// banner design section 4.2/5 require -- and then keeps raising it for the
// entire draft, since every poll re-delivers the same ~209 still-pending
// slots. An alarm that fires from the first second and never stops is worse
// than no alarm: it trains the user to ignore the one signal that means
// "your board is lying to you". So this function returns THREE groups, not
// two -- `{picks, pending, malformed}`:
//
//   - `picks`     entries with a player_key: a pick that has happened
//   - `pending`   entries with a pick + team_key but no player_key: a slot
//                 nobody has drafted into yet
//   - `malformed` entries that don't even yield a pick number and a team
//                 key -- genuinely unusable, and the ONLY thing allowed to
//                 raise the banner
//
// src/normalize.js exists precisely because Yahoo's v2 JSON is irregular --
// collections can arrive as a count-keyed numeric-key object, as a bare array
// of single-key-wrapped items, or spliced into the parent under numeric keys
// alongside real attributes. A draft_results collection with exactly one
// entry is the sharpest edge of that irregularity: normalize()'s own
// collection-unwrap logic only fires for a `count`-bearing object or a
// repeated-key array of 2+ items, so a single entry can legitimately reach
// this function either bare (`{pick, team_key, player_key}`) or still
// sitting under a wrapper key (`{draft_result: {...}}`).
//
// The response to all of that irregularity is the same as ever: never guess
// wrong, never throw, never drop.

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
 * Pull one entry apart into either a `pick` (pick + teamKey + playerKey) or
 * a `pending` slot (pick + teamKey, no playerKey). Field names for pick/
 * team_key/player_key accept a couple of plausible spellings, kept from
 * before this shape was verified; `round` is Yahoo's one verified spelling,
 * so no aliases for it. `round` is carried along when present but is never
 * required -- a missing or unparsable round does not make an otherwise-good
 * entry malformed, it just means the caller can't show one. Returns null --
 * never throws -- only when `pick` or `teamKey` (the two things needed to
 * place this entry in the draft order at all) cannot be found or coerced.
 */
function extractEntry(entry) {
  const pick = asPositiveInt(entry.pick ?? entry.pick_number ?? entry.pickNumber);
  const teamKey = asNonEmptyString(entry.team_key ?? entry.teamKey);
  if (pick === null || teamKey === null) return null;

  const playerKey = asNonEmptyString(entry.player_key ?? entry.playerKey);
  const round = asPositiveInt(entry.round);
  const base = round === null ? { pick, teamKey } : { pick, teamKey, round };

  return playerKey === null
    ? { kind: 'pending', value: base }
    : { kind: 'pick', value: { ...base, playerKey } };
}

/**
 * Normalise a draft_results payload into `{picks, pending, malformed}` (see
 * the module comment above for what each group means and why pending is not
 * malformed). Accepts an array of entries, a single entry not wrapped in a
 * list, an entry still sitting under a wrapper key, or the raw count-keyed
 * collection shape. Never throws: garbage in must not crash a draft-day
 * server, so anything that cannot be resolved to at least a pick number and
 * a team key is reported in `malformed` rather than thrown or dropped.
 */
export function parseDraftResults(payload) {
  const picks = [];
  const pending = [];
  const malformed = [];
  for (const raw of toEntryArray(payload)) {
    const unwrapped = unwrapSingleKeyObject(raw);
    const parsed = isPlainObject(unwrapped) ? extractEntry(unwrapped) : null;
    if (!parsed) { malformed.push(raw); continue; }
    (parsed.kind === 'pending' ? pending : picks).push(parsed.value);
  }
  return { picks, pending, malformed };
}

/**
 * Yahoo's draft_results lists every pick slot for the WHOLE draft from the
 * very first poll, made or pending (see module comment above) -- which means
 * Yahoo is already telling us, authoritatively, who is on the clock and
 * which pick is mine next. That is strictly better than deriving it
 * ourselves via `myPicks`/`nextPick` above: it is correct for a custom draft
 * order, third-round reversal, or any other league setting this module does
 * not model, and it needs no `teams`/`slot` at all.
 *
 * PRECEDENCE: this wins outright over the snake math whenever `pending` is
 * non-empty -- callers (src/draft-room.js's `deriveClock`) must try this
 * FIRST and only fall back to `myPicks`/`nextPick` when it returns null.
 * Returns null -- one unambiguous signal, never a partial answer -- when
 * `pending` is empty: predraft (draft_results not polled yet) or, from a
 * team's own perspective once none of its future picks remain pending, that
 * team's draft is over. Either way the caller has nothing to derive from
 * Yahoo's order and must fall back.
 */
export function clockFromPending(pending, myTeamKey) {
  if (!pending || pending.length === 0) return null;

  let onClock = pending[0];
  for (const p of pending) if (p.pick < onClock.pick) onClock = p;

  let mine = null;
  for (const p of pending) {
    if (p.teamKey === myTeamKey && (mine === null || p.pick < mine.pick)) mine = p;
  }

  return {
    currentPick: onClock.pick,
    currentRound: onClock.round ?? null,
    onTheClock: onClock.teamKey,
    myNextPick: mine ? mine.pick : null,
    myNextPickRound: mine ? (mine.round ?? null) : null,
  };
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
    // The `pending` group from the most recent parseDraftResults call (see
    // above) -- every pick slot Yahoo knows about that nobody has drafted
    // into yet. Empty predraft, or if a caller never supplies it.
    pending: [],
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
 * `currentPick`: when Yahoo's `pending` group is supplied and non-empty, the
 * lowest pending pick number IS the true current pick -- Yahoo's own
 * authoritative order (see `clockFromPending` above), which is preferred
 * outright. Only when `pending` is empty (predraft, or a caller that hasn't
 * supplied it) does this fall back to `drafted.size + 1` -- the COUNT of
 * distinct picks recorded, rather than `(max pick number seen) + 1`,
 * because Yahoo may deliver picks out of order (a slow poll catching two
 * picks in one response, or a retry racing a newer request), and a
 * max-based counter would report the wrong "on the clock" pick if a
 * lower-numbered pick becomes known after a higher-numbered one already is.
 *
 * Reconciliation: a Yahoo pick for a player already manually marked (see
 * markTaken) drops that player's manual mark instead of layering a second
 * "taken" record on top of it. Yahoo is authoritative, so after this the
 * player is recorded exactly once, in `drafted`, and is no longer reachable
 * by `undo` -- exactly as a real pick should be.
 */
export function applyPicks(state, picks, { myTeamKey, pending } = {}) {
  const drafted = new Map(state.drafted);
  const manualMarks = [...state.manualMarks];
  const available = new Set(state.available);
  const resolvedMyTeamKey = myTeamKey ?? state.myTeamKey ?? null;
  const resolvedPending = (pending ?? []).filter((p) => p && typeof p.pick === 'number' && p.teamKey);

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

  // PRECEDENCE: Yahoo's published order (the lowest still-pending pick)
  // beats our own count-based model of it -- see this function's docstring.
  const currentPick = resolvedPending.length > 0
    ? Math.min(...resolvedPending.map((p) => p.pick))
    : drafted.size + 1;
  const myRoster = [...drafted.values()]
    .filter((d) => resolvedMyTeamKey && d.teamKey === resolvedMyTeamKey)
    .sort((a, b) => a.pick - b.pick)
    .map((d) => d.playerId);

  return {
    ...state, available, drafted, manualMarks, currentPick, myRoster, myTeamKey: resolvedMyTeamKey, pending: resolvedPending,
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
