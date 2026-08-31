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

/** A Yahoo collection: numeric-string keys alongside a `count` sibling. */
function isCollection(v) {
  if (!isPlainObject(v)) return false;
  const keys = Object.keys(v);
  return keys.includes('count') && keys.some((k) => /^\d+$/.test(k));
}

/** Collection items arrive wrapped as `{ team: … }` / `{ player: … }`. */
function unwrapSingleKey(v) {
  if (!isPlainObject(v)) return v;
  const keys = Object.keys(v);
  return keys.length === 1 ? v[keys[0]] : v;
}

function normalizeValue(value) {
  if (Array.isArray(value)) {
    // An empty collection arrives as a literal []. Preserve its array-ness so
    // consumers can iterate; collapsing it to {} breaks every caller.
    if (value.length === 0) return [];
    // Yahoo nests the attribute list one level deeper: `team: [[ … ]]`.
    // Flattening once turns both shapes into a single list of parts to merge.
    return normalizeObject(flattenAttrs(value.flat()));
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
