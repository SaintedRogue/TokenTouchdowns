import { test } from 'node:test';
import assert from 'node:assert/strict';
import { yahooPlayerId, buildCrosswalk, lookupByYahooKey } from '../src/identity.js';

const RECORDS = [
  { yahooId: '26686', sleeperId: '1466', gsisId: '00-0030506', name: 'Travis Kelce', position: 'TE' },
  { yahooId: '30977', sleeperId: '4034', gsisId: '00-0033280', name: 'Christian McCaffrey', position: 'RB' },
];

test('yahooPlayerId extracts the numeric id from a player_key', () => {
  assert.equal(yahooPlayerId('470.p.26686'), '26686');
});

test('yahooPlayerId returns null for a key that is not a player key', () => {
  assert.equal(yahooPlayerId('470.l.1433971.t.4'), null);
  assert.equal(yahooPlayerId(undefined), null);
});

test('buildCrosswalk indexes records by yahoo id', () => {
  const cw = buildCrosswalk(RECORDS);
  assert.equal(cw.get('26686').sleeperId, '1466');
  assert.equal(cw.size, 2);
});

test('lookupByYahooKey resolves a Yahoo player_key to the crosswalk record', () => {
  const cw = buildCrosswalk(RECORDS);
  assert.equal(lookupByYahooKey(cw, '470.p.26686').gsisId, '00-0030506');
});

test('lookupByYahooKey returns null for an unknown player', () => {
  assert.equal(lookupByYahooKey(buildCrosswalk(RECORDS), '470.p.99999'), null);
});

import { normalizeName, buildAdpIndex, matchAdp } from '../src/identity.js';

test('normalizeName lowercases and strips punctuation', () => {
  assert.equal(normalizeName("A.J. Brown"), 'aj brown');
});

test('normalizeName strips generational suffixes', () => {
  assert.equal(normalizeName('James Cook III'), 'james cook');
  assert.equal(normalizeName('Odell Beckham Jr.'), 'odell beckham');
  assert.equal(normalizeName('Michael Pittman Sr'), 'michael pittman');
});

test('normalizeName collapses repeated whitespace', () => {
  assert.equal(normalizeName('  Travis   Kelce '), 'travis kelce');
});

test('matchAdp joins on normalised name and position', () => {
  const idx = buildAdpIndex([{ name: 'Travis Kelce', position: 'TE', adp: 40.2 }]);
  assert.equal(matchAdp(idx, { name: 'Travis Kelce', position: 'TE' }).adp, 40.2);
});

test('matchAdp matches across a suffix difference between sources', () => {
  // FFC says "James Cook", Yahoo says "James Cook III".
  const idx = buildAdpIndex([{ name: 'James Cook', position: 'RB', adp: 22.5 }]);
  assert.equal(matchAdp(idx, { name: 'James Cook III', position: 'RB' }).adp, 22.5);
});

test('matchAdp refuses to match across positions', () => {
  const idx = buildAdpIndex([{ name: 'Mike Williams', position: 'WR', adp: 90 }]);
  assert.equal(matchAdp(idx, { name: 'Mike Williams', position: 'TE' }), null);
});

test('matchAdp returns null when a name+position is ambiguous', () => {
  // Two distinct players share a normalised name AND position: never guess.
  const idx = buildAdpIndex([
    { name: 'Josh Allen', position: 'QB', adp: 30, team: 'BUF' },
    { name: 'Josh Allen', position: 'QB', adp: 200, team: 'JAX' },
  ]);
  assert.equal(matchAdp(idx, { name: 'Josh Allen', position: 'QB' }), null);
});

test('matchAdp resolves an otherwise-ambiguous name using team as a tiebreaker', () => {
  const idx = buildAdpIndex([
    { name: 'Josh Allen', position: 'QB', adp: 30, team: 'BUF' },
    { name: 'Josh Allen', position: 'QB', adp: 200, team: 'JAX' },
  ]);
  assert.equal(matchAdp(idx, { name: 'Josh Allen', position: 'QB', team: 'BUF' }).adp, 30);
});

test('matchAdp returns null for an unknown player rather than throwing', () => {
  assert.equal(matchAdp(buildAdpIndex([]), { name: 'Nobody', position: 'WR' }), null);
});
