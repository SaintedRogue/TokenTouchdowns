/** Yahoo player keys look like `<game_key>.p.<player_id>`. */
export function yahooPlayerId(playerKey) {
  const m = /^\d+\.p\.(\d+)$/.exec(playerKey ?? '');
  return m ? m[1] : null;
}

export function buildCrosswalk(records) {
  const map = new Map();
  for (const r of records ?? []) if (r?.yahooId) map.set(String(r.yahooId), r);
  return map;
}

export function lookupByYahooKey(crosswalk, playerKey) {
  const id = yahooPlayerId(playerKey);
  return id ? crosswalk.get(id) ?? null : null;
}

const SUFFIXES = new Set(['jr', 'sr', 'ii', 'iii', 'iv', 'v']);

// Sources spell positions differently for the same role. FFC uses "PK" for
// kickers where Yahoo uses "K"; some feeds use "DST" or "D/ST" for defenses
// where the DEF-by-team path below expects "DEF". Reconciled on both the
// index and lookup sides so neither side has to already agree with the other.
const POSITION_ALIASES = { PK: 'K', DST: 'DEF', 'D/ST': 'DEF' };

const stripSeparator = (s) => String(s ?? '').replace(/\|/g, '');

/**
 * Fold diacritics, lowercase, drop punctuation, drop generational suffixes,
 * collapse whitespace. Diacritic folding runs BEFORE punctuation stripping so
 * "Piñeiro" becomes "pineiro" (NFD splits n-with-tilde into "n" plus a
 * combining mark, which the punctuation strip then removes) rather than
 * "pieiro" -- sources that transliterate the same name differently must still
 * land on the same key.
 */
export function normalizeName(name) {
  return String(name ?? '')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z\s]/g, '')
    .split(/\s+/)
    .filter((w) => w && !SUFFIXES.has(w))
    .join(' ');
}

const normalizePosition = (position) => {
  const p = stripSeparator(position).toUpperCase();
  return POSITION_ALIASES[p] ?? p;
};

const normalizeTeam = (team) => stripSeparator(team).toUpperCase();

// Three disjoint, prefix-tagged key namespaces. Position and team are
// stripped of '|' above (belt-and-braces: normalizeName's punctuation strip
// already removes '|' from names), and the fixed prefix literal below means
// no combination of a forged position/team string can ever land inside a
// different namespace -- a query with position "QB|BUF" cannot address the
// internal team-qualified key for QB/BUF.
const baseKey = (pos, normName) => `BASE|${pos}|${normName}`;
const teamKey = (pos, normName, normTeam) => `TEAM|${pos}|${normName}|${normTeam}`;
const defKey = (normTeam) => `DEF|${normTeam}`;

/**
 * Set `key` to `record`, or to `null` if `key` already holds something -- the
 * marker for "two distinct source rows collided here, never guess which one
 * is right". Applied identically to the base key and the team-qualified key:
 * two rows that share name+position+team are exactly as ambiguous as two
 * rows that share name+position with different teams, and must not silently
 * resolve to whichever row was read last.
 */
function setOrMarkAmbiguous(index, key, record) {
  index.set(key, index.has(key) ? null : record);
}

/**
 * Index ADP records for lookup by `matchAdp` / `adpMatchState`.
 *
 * - Team defenses (spec rule 5) are keyed by team abbreviation ONLY, never by
 *   name. FFC names them "Seattle Defense" / "NY Giants Defense"; Yahoo's own
 *   defense names are not reliably the same strings. The team abbreviation
 *   is. A DEF row with no team abbreviation can never be safely keyed and is
 *   dropped rather than guessed at.
 * - A record whose normalised name is empty -- blank, all punctuation, or
 *   nothing left after suffix-stripping (a bare "III") -- is dropped outright
 *   for every non-DEF position. Indexing an empty name would let one
 *   malformed source row become a join key that matches every other
 *   malformed query, attaching a real ADP to nothing in particular.
 * - The same applies to an empty position, for the same reason: position is
 *   half of every non-DEF key, so a missing one is a live join key that
 *   collides with every other position-less row rather than an absent one.
 * - Every remaining record is indexed under its base key (name + position).
 *   Records that also carry a team are additionally indexed under a
 *   team-qualified key so an exact team can act as a tiebreaker. Both key
 *   kinds share the same ambiguity marker on collision.
 */
export function buildAdpIndex(records) {
  const index = new Map();
  for (const r of records ?? []) {
    const pos = normalizePosition(r?.position);
    // An absent position is not a position: dropped rather than indexed under
    // an empty one, exactly as an empty name is below.
    if (!pos) continue;

    if (pos === 'DEF') {
      const t = normalizeTeam(r?.team);
      if (!t) continue;
      setOrMarkAmbiguous(index, defKey(t), r);
      continue;
    }

    const n = normalizeName(r?.name);
    if (!n) continue;

    setOrMarkAmbiguous(index, baseKey(pos, n), r);

    const t = normalizeTeam(r?.team);
    if (t) setOrMarkAmbiguous(index, teamKey(pos, n, t), r);
  }
  return index;
}

/**
 * Resolve a query against an ADP index. Returns one of three states rather
 * than record-or-null so callers can tell "we have no ADP for this player"
 * (absent) apart from "we refused to guess" (ambiguous) -- both collapse to
 * `null` in `matchAdp`'s return value, but match-rate reporting needs to
 * tell them apart.
 */
function resolve(index, { name, position, team } = {}) {
  const pos = normalizePosition(position);
  // Mirrors buildAdpIndex: a query with no position cannot be keyed, and
  // must report absent rather than probing an empty-position namespace.
  if (!pos) return { status: 'absent' };

  if (pos === 'DEF') {
    // Team defenses never fall back to name matching (spec rule 5): no team
    // abbreviation on the query means there is nothing safe to key on.
    const t = normalizeTeam(team);
    if (!t) return { status: 'absent' };
    const key = defKey(t);
    if (!index.has(key)) return { status: 'absent' };
    const record = index.get(key);
    return record === null ? { status: 'ambiguous' } : { status: 'matched', record };
  }

  const n = normalizeName(name);
  if (!n) return { status: 'absent' };

  const t = normalizeTeam(team);
  if (t) {
    const tKey = teamKey(pos, n, t);
    // A known team-qualified key is authoritative: whether it holds a record
    // or the ambiguity marker, that answer stands. Falling through to the
    // (necessarily less specific) base key here would either be redundant,
    // or -- for a genuinely ambiguous team-qualified key -- would silently
    // relitigate a question that has already been answered "never guess".
    if (index.has(tKey)) {
      const record = index.get(tKey);
      return record === null ? { status: 'ambiguous' } : { status: 'matched', record };
    }
  }

  const bKey = baseKey(pos, n);
  if (!index.has(bKey)) return { status: 'absent' };
  const record = index.get(bKey);
  return record === null ? { status: 'ambiguous' } : { status: 'matched', record };
}

/**
 * Look up ADP for a player. Returns null rather than a best guess: a wrong ADP
 * on a plausible player is worse than a missing one, because it gets drafted
 * on. `team` is a tiebreaker only for regular players -- sources disagree on
 * team during preseason trades, so a non-matching team never suppresses an
 * otherwise-unambiguous match. Team defenses are the one exception (see
 * `buildAdpIndex`): they are keyed by team abbreviation only and never fall
 * back to name matching.
 */
export function matchAdp(index, query) {
  const r = resolve(index, query);
  return r.status === 'matched' ? r.record : null;
}

/**
 * Like `matchAdp`, but reports WHY a lookup produced no record: `'matched'`,
 * `'ambiguous'` (a real collision was refused, not guessed), or `'absent'`
 * (no source data at all). Match-rate reporting needs this distinction.
 * Additive: `matchAdp`'s existing `record | null` contract is unchanged, and
 * other code should keep using it.
 */
export function adpMatchState(index, query) {
  return resolve(index, query).status;
}
