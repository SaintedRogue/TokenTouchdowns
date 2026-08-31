import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { meta, normalize, fetchRaw, SCORING_FORMATS } from '../src/sources/ffc.js';

const raw = () => JSON.parse(readFileSync(
  new URL('./fixtures/ffc-adp.json', import.meta.url), 'utf8'));

test('meta declares adp and marks the join as fuzzy', () => {
  assert.equal(meta.name, 'ffc');
  assert.deepEqual(meta.provides, ['adp']);
  assert.equal(meta.joinKey, 'fuzzy');
  assert.equal(meta.ttlHours, 12);
});

test('normalize returns one record per player with ADP fields', () => {
  const out = normalize(raw()).records;
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
  assert.deepEqual(normalize({ status: 'Success' }), { meta: null, records: [] });
});

test('normalize preserves the feed meta that names which ADP this is', () => {
  // FFC serves a DIFFERENT dataset per scoring format. Dropping meta left the
  // cached numbers unlabelled, so a Non-PPR board could be read as Half-PPR.
  const { meta: m } = normalize(raw());
  assert.equal(m.type, 'Non-PPR');
  assert.equal(m.teams, 12);
  assert.equal(m.totalDrafts, 1884);
  assert.equal(m.startDate, '2026-08-24');
  assert.equal(m.endDate, '2026-08-31');
});

test('normalize reports a null meta rather than inventing one when FFC omits it', () => {
  assert.equal(normalize({ players: [] }).meta, null);
});

test('normalize preserves DEF and PK rows with their team abbreviations', () => {
  const out = normalize(raw()).records;
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

test('fetchRaw honours a non-default scoring format and league size', async () => {
  // These arguments were unreachable: `sync` passed neither, so the module
  // defaults (12-team Non-PPR) were the only board anyone could ever fetch.
  let seen;
  await fetchRaw({ season: 2026, teams: 10, scoring: 'half-ppr',
    fetch: async (url) => { seen = url; return { ok: true, json: async () => ({}) }; } });
  const u = new URL(seen);
  assert.equal(u.pathname, '/api/v1/adp/half-ppr');
  assert.equal(u.searchParams.get('teams'), '10');
  assert.equal(u.searchParams.get('year'), '2026');
});

test('SCORING_FORMATS names the formats the CLI offers', () => {
  assert.deepEqual(SCORING_FORMATS, ['standard', 'half-ppr', 'ppr']);
});

test('fetchRaw throws a clear error on a non-OK response', async () => {
  await assert.rejects(
    () => fetchRaw({ fetch: async () => ({ ok: false, status: 500 }) }), /500/);
});
