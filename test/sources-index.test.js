import { test } from 'node:test';
import assert from 'node:assert/strict';
import { SOURCES, sourcesProviding, allCapabilities } from '../src/sources/index.js';

test('every registered source exposes the required interface', () => {
  assert.ok(SOURCES.length >= 2);
  for (const s of SOURCES) {
    assert.equal(typeof s.meta.name, 'string');
    assert.ok(Array.isArray(s.meta.provides));
    assert.equal(typeof s.fetchRaw, 'function');
    assert.equal(typeof s.normalize, 'function');
  }
});

test('sourcesProviding finds a source by capability, not by name', () => {
  assert.deepEqual(sourcesProviding('adp').map((s) => s.meta.name), ['ffc']);
  assert.deepEqual(sourcesProviding('injury').map((s) => s.meta.name), ['sleeper']);
});

test('sourcesProviding returns an empty array for an unknown capability', () => {
  assert.deepEqual(sourcesProviding('nonsense'), []);
});

test('allCapabilities lists every capability exactly once, sorted', () => {
  const caps = allCapabilities();
  assert.deepEqual(caps, [...new Set(caps)].sort());
  assert.ok(caps.includes('adp'));
  assert.ok(caps.includes('injury'));
});

test('allCapabilities collapses a capability provided by two sources into one entry', () => {
  // The real registry has no overlapping capabilities, so dedup cannot be
  // exercised against it. Two stubs sharing 'adp' pin the guarantee.
  const stubs = [
    { meta: { name: 'a', provides: ['adp', 'injury'] } },
    { meta: { name: 'b', provides: ['adp'] } },
  ];
  assert.deepEqual(allCapabilities(stubs), ['adp', 'injury']);
});
