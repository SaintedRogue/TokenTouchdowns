import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { meta, normalize, fetchRaw } from '../src/sources/ffc.js';

const raw = () => JSON.parse(readFileSync(
  new URL('./fixtures/ffc-adp.json', import.meta.url), 'utf8'));

test('meta declares adp and marks the join as fuzzy', () => {
  assert.equal(meta.name, 'ffc');
  assert.deepEqual(meta.provides, ['adp']);
  assert.equal(meta.joinKey, 'fuzzy');
  assert.equal(meta.ttlHours, 12);
});

test('normalize returns one record per player with ADP fields', () => {
  const out = normalize(raw());
  assert.equal(out.length, 4);
  const gibbs = out.find((p) => p.name === 'Jahmyr Gibbs');
  assert.equal(gibbs.adp, 1.4);
  assert.equal(gibbs.position, 'RB');
  assert.equal(gibbs.team, 'DET');
  assert.equal(gibbs.timesDrafted, 338);
  assert.equal(gibbs.high, 1);
  assert.equal(gibbs.low, 3);
  assert.equal(gibbs.stdev, 0.6);
  assert.equal(gibbs.bye, 6);
});

test('normalize tolerates a response with no players array', () => {
  assert.deepEqual(normalize({ status: 'Success' }), []);
});

test('normalize preserves DEF and PK rows with their team abbreviations', () => {
  const out = normalize(raw());
  const def = out.find((p) => p.position === 'DEF');
  const pk = out.find((p) => p.position === 'PK');
  assert.equal(def.team, 'SEA');
  assert.equal(def.name, 'Seattle Defense');
  assert.equal(pk.team, 'DAL');
  assert.equal(pk.adp, 135.8);
});

test('fetchRaw builds the documented ADP URL with season and format', async () => {
  let seen;
  await fetchRaw({ season: 2026, teams: 12, scoring: 'standard',
    fetch: async (url) => { seen = url; return { ok: true, json: async () => ({}) }; } });
  const u = new URL(seen);
  assert.equal(u.hostname, 'fantasyfootballcalculator.com');
  assert.equal(u.pathname, '/api/v1/adp/standard');
  assert.equal(u.searchParams.get('teams'), '12');
  assert.equal(u.searchParams.get('year'), '2026');
});

test('fetchRaw throws a clear error on a non-OK response', async () => {
  await assert.rejects(
    () => fetchRaw({ fetch: async () => ({ ok: false, status: 500 }) }), /500/);
});
