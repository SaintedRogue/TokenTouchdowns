import { test } from 'node:test';
import assert from 'node:assert/strict';
import { flattenAttrs } from '../src/normalize.js';

test('flattenAttrs merges an array of single-key objects into one object', () => {
  const input = [{ team_key: '470.l.1.t.1' }, { team_id: '1' }, { name: 'Any Given Model' }];
  assert.deepEqual(flattenAttrs(input), {
    team_key: '470.l.1.t.1', team_id: '1', name: 'Any Given Model',
  });
});

test('flattenAttrs skips the empty arrays Yahoo interleaves between attributes', () => {
  // Real fixture data has [] at positions 3 and 6 of the team attribute list.
  const input = [{ team_key: 'k' }, [], { name: 'n' }, []];
  assert.deepEqual(flattenAttrs(input), { team_key: 'k', name: 'n' });
});

import { collectionToArray } from '../src/normalize.js';

test('collectionToArray converts numeric-string-keyed objects to an array', () => {
  const input = { 0: { name: 'a' }, 1: { name: 'b' }, count: 2 };
  assert.deepEqual(collectionToArray(input), [{ name: 'a' }, { name: 'b' }]);
});

test('collectionToArray drops the count sibling rather than treating it as an item', () => {
  const input = { 0: { name: 'a' }, count: 1 };
  const out = collectionToArray(input);
  assert.equal(out.length, 1);
  assert.deepEqual(out, [{ name: 'a' }]);
});

test('collectionToArray orders keys numerically, not lexicographically', () => {
  // Naive Object.keys ordering would yield 0,1,10,2 -- wrong past 10 players.
  const input = {};
  for (let i = 0; i < 12; i++) input[i] = { n: i };
  input.count = 12;
  assert.deepEqual(collectionToArray(input).map(x => x.n),
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]);
});

test('collectionToArray returns an empty array for an empty collection', () => {
  assert.deepEqual(collectionToArray({ count: 0 }), []);
});
