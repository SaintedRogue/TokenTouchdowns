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

test('normalize trims whitespace off gsisId so the nflverse join cannot miss silently', () => {
  // Sleeper's live feed really does this: 855 of 3,875 records carrying a
  // gsis_id ship it with a LEADING SPACE (" 00-0023177"). nflverse player_ids
  // have none, so every one of those records fails an exact-match join --
  // silently, because a missed join looks identical to a player who simply is
  // not in the crosswalk. It cost the draft room 10 of 210 real drafted picks.
  //
  // Exactly the failure this function's own docstring already describes for
  // yahooId ("a type mismatch makes every join miss silently"), so the fix is
  // the same shape: normalise at the boundary, once, where the data enters.
  const out = normalize({
    '1': { player_id: '1', yahoo_id: 111, gsis_id: ' 00-0023177',
           full_name: 'Leading Space', position: 'RB', team: 'DET' },
    '2': { player_id: '2', yahoo_id: 222, gsis_id: '00-0033280  ',
           full_name: 'Trailing Space', position: 'WR', team: 'CIN' },
    '3': { player_id: '3', yahoo_id: 333, gsis_id: '   ',
           full_name: 'Only Whitespace', position: 'QB', team: 'BUF' },
  });
  const by = (n) => out.find((p) => p.name === n);
  assert.equal(by('Leading Space').gsisId, '00-0023177');
  assert.equal(by('Trailing Space').gsisId, '00-0033280');
  // A whitespace-only id carries no identity; it must become null rather than
  // an empty string that would join against nothing while looking present.
  assert.equal(by('Only Whitespace').gsisId, null);
});
