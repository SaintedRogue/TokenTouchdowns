import * as sleeper from './sleeper.js';
import * as ffc from './ffc.js';

export const SOURCES = [sleeper, ffc];

/** Look up by capability so a provider can change without changing callers. */
export function sourcesProviding(capability) {
  return SOURCES.filter((s) => s.meta.provides.includes(capability));
}

export function allCapabilities() {
  return [...new Set(SOURCES.flatMap((s) => s.meta.provides))].sort();
}
