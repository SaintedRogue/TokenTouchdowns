import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { meta, normalize, fetchRaw } from '../src/sources/sleeper.js';

const raw = () => JSON.parse(readFileSync(
  new URL('./fixtures/sleeper-players.json', import.meta.url), 'utf8'));

test('meta declares the capabilities and join key the registry needs', () => {
  assert.equal(meta.name, 'sleeper');
  assert.equal(meta.joinKey, 'yahoo_id');
  assert.ok(meta.provides.includes('injury'));
  assert.ok(meta.provides.includes('identity'));
  assert.equal(meta.documented, true);
  assert.equal(typeof meta.ttlHours, 'number');
});

test('normalize keeps only players that carry a yahoo_id', () => {
  // 6,750 of 12,225 live players have one; the rest cannot be joined.
  const out = normalize(raw());
  assert.equal(out.length, 2);
  assert.deepEqual(out.map((p) => p.name).sort(),
    ['Christian McCaffrey', 'Travis Kelce']);
});

test('normalize coerces yahooId to a string', () => {
  // Sleeper sends a number; Yahoo player_key parses to a string. Mismatched
  // types make every join silently return zero matches.
  const kelce = normalize(raw()).find((p) => p.name === 'Travis Kelce');
  assert.equal(kelce.yahooId, '26686');
  assert.equal(typeof kelce.yahooId, 'string');
});

test('normalize carries the ids and injury fields enrichment needs', () => {
  const cmc = normalize(raw()).find((p) => p.name === 'Christian McCaffrey');
  assert.equal(cmc.sleeperId, '4034');
  assert.equal(cmc.gsisId, '00-0033280');
  assert.equal(cmc.espnId, '3116385');
  assert.equal(cmc.position, 'RB');
  assert.equal(cmc.team, 'SF');
  assert.equal(cmc.injuryStatus, 'Questionable');
  assert.equal(cmc.depthChartOrder, 1);
});

test('fetchRaw requests the documented players endpoint', async () => {
  let seen;
  await fetchRaw({ fetch: async (url) => { seen = url; return { ok: true, json: async () => ({}) }; } });
  assert.equal(seen, 'https://api.sleeper.app/v1/players/nfl');
});

test('fetchRaw throws a clear error on a non-OK response', async () => {
  await assert.rejects(
    () => fetchRaw({ fetch: async () => ({ ok: false, status: 503 }) }),
    /503/);
});

test('normalize keeps a player whose yahoo_id is 0 rather than treating it as absent', () => {
  // Guards against a regression to a truthiness check (`if (!p.yahoo_id)`),
  // which would silently drop a player with id 0. The filter must be nullish.
  const out = normalize({
    zero: {
      player_id: '9999', yahoo_id: 0, espn_id: null, gsis_id: null,
      full_name: 'Zero Id Player', position: 'WR', team: 'NYJ',
      injury_status: null, status: 'Active', depth_chart_order: 2,
    },
  });
  assert.equal(out.length, 1);
  assert.equal(out[0].yahooId, '0');
});
