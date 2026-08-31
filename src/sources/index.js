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
 */
export function allCapabilities(sources = SOURCES) {
  return [...new Set(sources.flatMap((s) => s.meta.provides))].sort();
}
