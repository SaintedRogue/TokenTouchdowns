import * as sleeper from './sleeper.js';
import * as ffc from './ffc.js';

export const SOURCES = [sleeper, ffc];

/** Look up by capability so a provider can change without changing callers. */
export function sourcesProviding(capability) {
  return SOURCES.filter((s) => s.meta.provides.includes(capability));
}

/**
 * Lists all capabilities provided by any registered source, sorted, each unique.
 * Accepts an optional sources array to allow testing with custom source sets.
 *
 * Note this is the set of capabilities the SOURCES can supply, which is a
 * superset of what enrichment actually implements -- see
 * IMPLEMENTED_CAPABILITIES in enrich.js, which is what `--with` validates
 * against so the CLI never accepts a flag that does nothing.
 */
export function allCapabilities(sources = SOURCES) {
  return [...new Set(sources.flatMap((s) => s.meta.provides))].sort();
}

/**
 * A normalize() result -- and therefore a cache payload -- is either a bare
 * array of records or `{ meta, records }` for a source that carries
 * feed-level metadata worth keeping (FFC's scoring variant). Cache files
 * predate the second shape and are not versioned, so both must stay readable.
 *
 * Throws on anything else rather than returning []: a payload we cannot
 * recognise is a corrupt cache, and silently reading it as "zero records"
 * would present a broken cache as an empty ADP feed.
 */
export function recordsOf(payload) {
  if (Array.isArray(payload)) return payload;
  if (Array.isArray(payload?.records)) return payload.records;
  throw new TypeError('cache payload is neither a record array nor { meta, records }');
}

/** Feed-level metadata from a payload, or null for the bare-array shape. */
export function metaOf(payload) {
  if (Array.isArray(payload) || payload === null || typeof payload !== 'object') return null;
  return payload.meta ?? null;
}

/**
 * Short human label for which variant of a feed the cached records are, e.g.
 * "Half-PPR, 10-team". Returned wherever those numbers are shown: an ADP
 * column with no variant on it invites reading a Non-PPR board as a
 * Half-PPR one.
 */
export function feedVariantLabel(meta) {
  if (!meta) return null;
  const parts = [];
  if (meta.type) parts.push(String(meta.type));
  if (meta.teams) parts.push(`${meta.teams}-team`);
  return parts.length > 0 ? parts.join(', ') : null;
}
