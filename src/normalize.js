// Yahoo Fantasy v2 JSON is XML-shaped. This module converts it into ordinary
// JavaScript objects at the boundary so the shape never leaks further in.

/**
 * Merge Yahoo's array-of-single-key-objects into one flat object.
 * Empty arrays are interleaved between real attributes and are skipped.
 */
export function flattenAttrs(items) {
  const out = {};
  for (const item of items) {
    if (Array.isArray(item)) continue;
    Object.assign(out, item);
  }
  return out;
}

/**
 * Convert Yahoo's numeric-string-keyed pseudo-array into a real array.
 * `{ "0": x, "1": y, count: 2 }` -> `[x, y]`.
 * Keys are sorted numerically: lexicographic order breaks past index 9.
 */
export function collectionToArray(collection) {
  if (!collection) return [];
  return Object.keys(collection)
    .filter((k) => /^\d+$/.test(k))
    .sort((a, b) => Number(a) - Number(b))
    .map((k) => collection[k]);
}

const META_KEYS = new Set(['xml:lang', 'yahoo:uri', 'copyright', 'refresh_rate', 'time']);

const isPlainObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

/**
 * A Yahoo collection: numeric-string keys, usually alongside a `count`
 * sibling -- but `count` is not load-bearing. Some collections (e.g. league
 * settings' roster_positions when it arrives object-shaped) carry only
 * numeric keys and no `count` at all. An object whose keys are ALL numeric
 * is unambiguously a collection either way; one with numeric keys PLUS real
 * attributes (e.g. `roster: { "0": {...}, coverage_type: "week" }`) is not --
 * that shape's numeric-keyed fragment gets merged up by normalizeObject
 * instead. `count` itself is ignored when checking "all numeric" so its
 * presence doesn't disqualify the classic shape.
 */
function isCollection(v) {
  if (!isPlainObject(v)) return false;
  const keys = Object.keys(v).filter((k) => k !== 'count');
  return keys.length > 0 && keys.every((k) => /^\d+$/.test(k));
}

/** Collection items arrive wrapped as `{ team: … }` / `{ player: … }`. */
function unwrapSingleKey(v) {
  if (!isPlainObject(v)) return v;
  const keys = Object.keys(v);
  return keys.length === 1 ? v[keys[0]] : v;
}

/**
 * A Yahoo collection can also arrive as a bare JSON array of items that all
 * share the exact same single wrapper key -- e.g. league settings'
 * roster_positions: `[{roster_position: X}, {roster_position: Y}, ...]`,
 * with no numeric-keyed object and no `count` anywhere. That is
 * indistinguishable in shape from Yahoo's attribute-fragment arrays (e.g.
 * `team: [[{team_key: …}, {name: …}, …]]`) EXCEPT that attribute fragments
 * never repeat a key, while these collection items always repeat the same
 * one. Requiring 2+ items keeps single-item wrapped values (e.g.
 * `managers: [{manager: X}]`) flattening to a plain object as before, since
 * a lone item is ambiguous and every real single-item case in the fixtures
 * is meant to stay an object.
 */
function isRepeatedKeyArray(items) {
  const objs = items.filter((it) => !Array.isArray(it));
  if (objs.length < 2) return false;
  let sharedKey;
  for (const item of objs) {
    if (!isPlainObject(item)) return false;
    const keys = Object.keys(item);
    if (keys.length !== 1) return false;
    if (sharedKey === undefined) sharedKey = keys[0];
    else if (keys[0] !== sharedKey) return false;
  }
  return true;
}

function normalizeValue(value) {
  if (Array.isArray(value)) {
    // An empty collection arrives as a literal []. Preserve its array-ness so
    // consumers can iterate; collapsing it to {} breaks every caller.
    if (value.length === 0) return [];
    // Yahoo nests the attribute list one level deeper: `team: [[ … ]]`.
    // Flattening once turns both shapes into a single list of parts to merge.
    const flat = value.flat();
    if (isRepeatedKeyArray(flat)) {
      return flat
        .filter((it) => !Array.isArray(it))
        .map((item) => normalizeValue(unwrapSingleKey(item)));
    }
    return normalizeObject(flattenAttrs(flat));
  }
  if (isCollection(value)) {
    return collectionToArray(value).map((item) => normalizeValue(unwrapSingleKey(item)));
  }
  if (isPlainObject(value)) return normalizeObject(value);
  return value;
}

function normalizeObject(obj) {
  const out = {};
  for (const [k, v] of Object.entries(obj)) {
    if (META_KEYS.has(k)) continue;
    // Yahoo also splices payload fragments under numeric keys ALONGSIDE plain
    // attributes, with no `count` sibling -- e.g.
    //   roster: { "0": { players: [] }, coverage_type: "week", week: 1 }
    // Those fragments belong to the parent, so merge them up rather than
    // leaving a meaningless "0" key behind.
    if (/^\d+$/.test(k)) {
      Object.assign(out, normalizeValue(v));
      continue;
    }
    out[k] = normalizeValue(v);
  }
  return out;
}

/**
 * Convert a raw Yahoo Fantasy v2 response into ordinary JS objects.
 * Collections become arrays; attribute lists become plain objects; the
 * `fantasy_content` envelope and XML metadata keys are stripped.
 */
export function normalize(raw) {
  const content = raw?.fantasy_content ?? raw;
  return normalizeObject(content);
}
