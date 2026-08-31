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
