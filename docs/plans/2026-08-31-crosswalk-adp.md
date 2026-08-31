# Crosswalk + ADP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Join Sleeper injury data and Fantasy Football Calculator ADP onto Yahoo players, so `tt players --with=adp,injury` supports draft prep before the 2026-09-09 draft.

**Architecture:** Each external source is a module under `src/sources/` exporting `meta`, `fetchRaw`, and `normalize`. No source knows Yahoo exists — `identity.js` alone translates between identifier spaces, using a Sleeper-derived crosswalk (`yahoo_id` → Sleeper/gsis/espn ids). `enrich.js` joins source records onto Yahoo players. Everything is cached to JSON files with per-source TTLs.

**Tech Stack:** Node ≥22 ESM, `node:test` + `node:assert/strict`, zero new runtime dependencies. Injected `fetch` throughout, matching `src/client.js`.

**Spec:** `docs/multi-source-enrichment-design.md`

## Global Constraints

- Node ≥22, ESM only (`"type": "module"`). No new runtime dependencies.
- Tests use the built-in `node:test` runner. Run with `npm test`.
- TDD is mandatory: write the failing test, watch it fail, then implement.
- Cache directory: `~/.tokentouchdowns/cache/`, overridable via a `dir` option for tests. Never write cache files into the repo.
- TTLs: Sleeper players `24` hours; FFC ADP `12` hours.
- Sleeper: no auth. Stay under 1000 calls/min. Licensed **non-commercial**.
- **Sleeper `yahoo_id` is a NUMBER (`26686`); Yahoo's id parsed from `player_key` is a STRING (`"26686"`). Normalise to string on both sides or every join silently returns zero matches.**
- Fuzzy matching must **never guess**: ambiguity records an unmatched player.
- Fixtures must be scrubbed and deterministic (see `tools/capture-fixtures.mjs`).
- Out of scope: stats, projections, Sleeper as a league provider.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/cache.js` | TTL'd JSON file cache. Knows nothing about sources. |
| `src/sources/sleeper.js` | Fetch + normalize Sleeper `/players/nfl`. |
| `src/sources/ffc.js` | Fetch + normalize Fantasy Football Calculator ADP. |
| `src/sources/index.js` | Registry; capability lookup. |
| `src/identity.js` | Crosswalk build/lookup, name normalisation, fuzzy ADP index. |
| `src/enrich.js` | Join source records onto Yahoo players. |
| `src/cli.js` | Modify: `--with=`, `sync`, `sources` commands. |
| `test/fixtures/sleeper-players.json` | Trimmed, scrubbed Sleeper sample. |
| `test/fixtures/ffc-adp.json` | Trimmed FFC sample. |

---

### Task 1: TTL file cache

**Files:**
- Create: `src/cache.js`
- Test: `test/cache.test.js`

**Interfaces:**
- Consumes: nothing.
- Produces: `isStale(fetchedAt, ttlHours, now) -> boolean`, `readCache(name, {dir, ttlHours, now}) -> {data, fetchedAt, stale} | null`, `writeCache(name, data, {dir, now}) -> void`, `cachePath(name, dir) -> string`.

- [ ] **Step 1: Write the failing test**

```js
// test/cache.test.js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { readCache, writeCache, isStale } from '../src/cache.js';

const tmp = () => mkdtempSync(path.join(tmpdir(), 'tt-cache-'));

test('isStale is false inside the TTL window', () => {
  const now = 1_000_000_000_000;
  assert.equal(isStale(now - 60 * 60 * 1000, 24, now), false);
});

test('isStale is true past the TTL window', () => {
  const now = 1_000_000_000_000;
  assert.equal(isStale(now - 25 * 60 * 60 * 1000, 24, now), true);
});

test('readCache returns null when nothing has been written', async () => {
  const dir = tmp();
  assert.equal(await readCache('nope', { dir, ttlHours: 24 }), null);
  rmSync(dir, { recursive: true, force: true });
});

test('writeCache then readCache round-trips the data', async () => {
  const dir = tmp();
  await writeCache('demo', { a: 1 }, { dir, now: 5000 });
  const got = await readCache('demo', { dir, ttlHours: 24, now: 6000 });
  assert.deepEqual(got.data, { a: 1 });
  assert.equal(got.fetchedAt, 5000);
  assert.equal(got.stale, false);
  rmSync(dir, { recursive: true, force: true });
});

test('readCache still returns expired data, marked stale', async () => {
  // Stale data beats no data: enrichment degrades, it does not fail.
  const dir = tmp();
  await writeCache('demo', { a: 1 }, { dir, now: 0 });
  const got = await readCache('demo', { dir, ttlHours: 1, now: 2 * 60 * 60 * 1000 });
  assert.equal(got.stale, true);
  assert.deepEqual(got.data, { a: 1 });
  rmSync(dir, { recursive: true, force: true });
});

test('readCache returns null for corrupt cache content', async () => {
  const dir = tmp();
  const { writeFileSync } = await import('node:fs');
  writeFileSync(path.join(dir, 'broken.json'), '{not json');
  assert.equal(await readCache('broken', { dir, ttlHours: 24 }), null);
  rmSync(dir, { recursive: true, force: true });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test test/cache.test.js`
Expected: FAIL with `ERR_MODULE_NOT_FOUND` for `src/cache.js`.

- [ ] **Step 3: Write minimal implementation**

```js
// src/cache.js
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { homedir } from 'node:os';
import path from 'node:path';

export const CACHE_DIR =
  process.env.TT_CACHE_DIR || path.join(homedir(), '.tokentouchdowns', 'cache');

export function cachePath(name, dir = CACHE_DIR) {
  return path.join(dir, `${name}.json`);
}

export function isStale(fetchedAt, ttlHours, now = Date.now()) {
  return now - fetchedAt > ttlHours * 60 * 60 * 1000;
}

/**
 * Returns { data, fetchedAt, stale } or null when absent/corrupt.
 * Expired entries are returned WITH stale:true rather than discarded --
 * stale enrichment beats no enrichment.
 */
export async function readCache(name, { dir = CACHE_DIR, ttlHours, now = Date.now() } = {}) {
  try {
    const raw = await readFile(cachePath(name, dir), 'utf8');
    const parsed = JSON.parse(raw);
    if (typeof parsed?.fetchedAt !== 'number') return null;
    return { data: parsed.data, fetchedAt: parsed.fetchedAt,
             stale: isStale(parsed.fetchedAt, ttlHours, now) };
  } catch {
    return null;
  }
}

export async function writeCache(name, data, { dir = CACHE_DIR, now = Date.now() } = {}) {
  await mkdir(dir, { recursive: true });
  await writeFile(cachePath(name, dir), JSON.stringify({ fetchedAt: now, data }));
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test test/cache.test.js`
Expected: PASS, 6/6.

- [ ] **Step 5: Commit**

```bash
git add src/cache.js test/cache.test.js
git commit -m "feat: TTL'd JSON file cache for external sources"
```

---

### Task 2: Sleeper source module

**Files:**
- Create: `src/sources/sleeper.js`, `test/fixtures/sleeper-players.json`
- Test: `test/sources-sleeper.test.js`

**Interfaces:**
- Consumes: nothing.
- Produces: `meta` object, `fetchRaw({fetch}) -> object`, `normalize(raw) -> Array<{sleeperId, yahooId, gsisId, espnId, name, position, team, injuryStatus, depthChartOrder}>`. **`yahooId` is a STRING.**

- [ ] **Step 1: Create the fixture**

Write `test/fixtures/sleeper-players.json` — a trimmed, real-shaped sample. Field names and types match the live payload exactly (`yahoo_id` is a number).

```json
{
  "1466": {
    "player_id": "1466", "yahoo_id": 26686, "espn_id": 15847,
    "gsis_id": "00-0030506", "full_name": "Travis Kelce",
    "position": "TE", "team": "KC", "injury_status": null,
    "status": "Active", "depth_chart_order": 1
  },
  "4034": {
    "player_id": "4034", "yahoo_id": 30977, "espn_id": 3116385,
    "gsis_id": "00-0033280", "full_name": "Christian McCaffrey",
    "position": "RB", "team": "SF", "injury_status": "Questionable",
    "status": "Active", "depth_chart_order": 1
  },
  "0000": {
    "player_id": "0000", "yahoo_id": null, "espn_id": null,
    "gsis_id": null, "full_name": "No Yahoo Counterpart",
    "position": "LB", "team": "CHI", "injury_status": null,
    "status": "Active", "depth_chart_order": 3
  },
  "KC": {
    "player_id": "KC", "yahoo_id": null, "espn_id": null,
    "gsis_id": null, "full_name": null, "first_name": "Kansas City",
    "last_name": "Chiefs", "position": "DEF", "team": "KC",
    "injury_status": null, "status": "Active", "depth_chart_order": null
  }
}
```

- [ ] **Step 2: Write the failing test**

```js
// test/sources-sleeper.test.js
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `node --test test/sources-sleeper.test.js`
Expected: FAIL with `ERR_MODULE_NOT_FOUND` for `src/sources/sleeper.js`.

- [ ] **Step 4: Write minimal implementation**

```js
// src/sources/sleeper.js
const URL_PLAYERS = 'https://api.sleeper.app/v1/players/nfl';

export const meta = {
  name: 'sleeper',
  provides: ['identity', 'injury', 'depth'],
  joinKey: 'yahoo_id',
  ttlHours: 24,
  documented: true,
  license: 'non-commercial',
};

export async function fetchRaw({ fetch: fetchImpl = globalThis.fetch } = {}) {
  const res = await fetchImpl(URL_PLAYERS);
  if (!res.ok) throw new Error(`Sleeper players fetch failed: HTTP ${res.status}`);
  return res.json();
}

/**
 * Players without a yahoo_id cannot be joined to a Yahoo roster, so they are
 * dropped here rather than carried through the pipeline.
 * yahooId is coerced to a string: Sleeper sends a number, Yahoo's player_key
 * parses to a string, and a type mismatch makes every join miss silently.
 */
export function normalize(raw) {
  const out = [];
  for (const p of Object.values(raw ?? {})) {
    if (p?.yahoo_id === null || p?.yahoo_id === undefined) continue;
    out.push({
      sleeperId: String(p.player_id),
      yahooId: String(p.yahoo_id),
      gsisId: p.gsis_id ?? null,
      espnId: p.espn_id === null || p.espn_id === undefined ? null : String(p.espn_id),
      name: p.full_name ?? [p.first_name, p.last_name].filter(Boolean).join(' '),
      position: p.position ?? null,
      team: p.team ?? null,
      injuryStatus: p.injury_status ?? null,
      depthChartOrder: p.depth_chart_order ?? null,
    });
  }
  return out;
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `node --test test/sources-sleeper.test.js`
Expected: PASS, 6/6.

- [ ] **Step 6: Commit**

```bash
git add src/sources/sleeper.js test/sources-sleeper.test.js test/fixtures/sleeper-players.json
git commit -m "feat: Sleeper source module for identity and injury data"
```

---

### Task 3: Identity crosswalk

**Files:**
- Create: `src/identity.js`
- Test: `test/identity.test.js`

**Interfaces:**
- Consumes: `normalize()` output from Task 2.
- Produces: `yahooPlayerId(playerKey) -> string | null`, `buildCrosswalk(records) -> Map<string, record>`, `lookupByYahooKey(crosswalk, playerKey) -> record | null`.

- [ ] **Step 1: Write the failing test**

```js
// test/identity.test.js
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test test/identity.test.js`
Expected: FAIL with `ERR_MODULE_NOT_FOUND` for `src/identity.js`.

- [ ] **Step 3: Write minimal implementation**

```js
// src/identity.js

/** Yahoo player keys look like `<game_key>.p.<player_id>`. */
export function yahooPlayerId(playerKey) {
  const m = /^\d+\.p\.(\d+)$/.exec(playerKey ?? '');
  return m ? m[1] : null;
}

export function buildCrosswalk(records) {
  const map = new Map();
  for (const r of records ?? []) if (r?.yahooId) map.set(String(r.yahooId), r);
  return map;
}

export function lookupByYahooKey(crosswalk, playerKey) {
  const id = yahooPlayerId(playerKey);
  return id ? crosswalk.get(id) ?? null : null;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test test/identity.test.js`
Expected: PASS, 5/5.

- [ ] **Step 5: Commit**

```bash
git add src/identity.js test/identity.test.js
git commit -m "feat: Sleeper-derived Yahoo player crosswalk"
```

---

### Task 4: Name normalisation and the fuzzy ADP matcher

This is the riskiest component in the plan. It gets its own task and adversarial tests.

**Files:**
- Modify: `src/identity.js`
- Test: `test/identity.test.js`

**Interfaces:**
- Consumes: `buildCrosswalk` from Task 3.
- Produces: `normalizeName(name) -> string`, `buildAdpIndex(adpRecords) -> Map<string, record|null>` (a `null` value marks an ambiguous key), `matchAdp(index, {name, position}) -> record | null`.

- [ ] **Step 1: Write the failing test**

```js
// append to test/identity.test.js
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test test/identity.test.js`
Expected: FAIL with "does not provide an export named 'normalizeName'".

- [ ] **Step 3: Write minimal implementation**

```js
// append to src/identity.js

const SUFFIXES = new Set(['jr', 'sr', 'ii', 'iii', 'iv', 'v']);

/** Lowercase, drop punctuation, drop generational suffixes, collapse spaces. */
export function normalizeName(name) {
  return String(name ?? '')
    .toLowerCase()
    .replace(/[^a-z\s]/g, '')
    .split(/\s+/)
    .filter((w) => w && !SUFFIXES.has(w))
    .join(' ');
}

const keyOf = (name, position) => `${normalizeName(name)}|${String(position ?? '').toUpperCase()}`;

/**
 * Index ADP records by `name|POSITION`. A key colliding between two distinct
 * records stores `null` -- the marker for "ambiguous, never guess". The
 * colliding records are kept under a team-qualified key so an exact team
 * match can still resolve them.
 */
export function buildAdpIndex(records) {
  const index = new Map();
  for (const r of records ?? []) {
    const k = keyOf(r.name, r.position);
    index.set(k, index.has(k) ? null : r);
    if (r.team) index.set(`${k}|${String(r.team).toUpperCase()}`, r);
  }
  return index;
}

/**
 * Look up ADP for a player. Returns null rather than a best guess: a wrong ADP
 * on a plausible player is worse than a missing one, because it gets drafted on.
 */
export function matchAdp(index, { name, position, team } = {}) {
  const k = keyOf(name, position);
  if (team) {
    const exact = index.get(`${k}|${String(team).toUpperCase()}`);
    if (exact) return exact;
  }
  return index.get(k) ?? null;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test test/identity.test.js`
Expected: PASS, 14/14 (5 from Task 3 plus 9 here).

- [ ] **Step 5: Commit**

```bash
git add src/identity.js test/identity.test.js
git commit -m "feat: name normalisation and non-guessing fuzzy ADP matcher"
```

---

### Task 5: Fantasy Football Calculator ADP source

**Files:**
- Create: `src/sources/ffc.js`, `test/fixtures/ffc-adp.json`
- Test: `test/sources-ffc.test.js`

**Interfaces:**
- Consumes: nothing.
- Produces: `meta`, `fetchRaw({fetch, season, teams, scoring}) -> object`, `normalize(raw) -> Array<{name, position, team, adp, timesDrafted, high, low, stdev, bye}>`.

- [ ] **Step 1: Create the fixture**

Write `test/fixtures/ffc-adp.json`, matching the live response shape:

```json
{
  "status": "Success",
  "meta": { "type": "Non-PPR", "teams": 12, "rounds": 15, "total_drafts": 1884,
            "start_date": "2026-08-24", "end_date": "2026-08-31" },
  "players": [
    { "player_id": 5672, "name": "Jahmyr Gibbs", "position": "RB", "team": "DET",
      "adp": 1.4, "adp_formatted": "1.01", "times_drafted": 338,
      "high": 1, "low": 3, "stdev": 0.6, "bye": 6 },
    { "player_id": 1466, "name": "Travis Kelce", "position": "TE", "team": "KC",
      "adp": 40.2, "adp_formatted": "4.04", "times_drafted": 210,
      "high": 30, "low": 55, "stdev": 4.1, "bye": 5 }
  ]
}
```

- [ ] **Step 2: Write the failing test**

```js
// test/sources-ffc.test.js
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
  assert.equal(out.length, 2);
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `node --test test/sources-ffc.test.js`
Expected: FAIL with `ERR_MODULE_NOT_FOUND` for `src/sources/ffc.js`.

- [ ] **Step 4: Write minimal implementation**

```js
// src/sources/ffc.js
const BASE = 'https://fantasyfootballcalculator.com/api/v1/adp';

export const meta = {
  name: 'ffc',
  provides: ['adp'],
  // FFC exposes only its own player_id, with no external crosswalk, so ADP
  // must be matched on normalised name + position. See identity.js.
  joinKey: 'fuzzy',
  ttlHours: 12,
  documented: true,
  license: 'public',
};

export async function fetchRaw({
  fetch: fetchImpl = globalThis.fetch,
  season = new Date().getUTCFullYear(),
  teams = 12,
  scoring = 'standard',
} = {}) {
  const url = new URL(`${BASE}/${scoring}`);
  url.searchParams.set('teams', String(teams));
  url.searchParams.set('year', String(season));
  const res = await fetchImpl(url.toString());
  if (!res.ok) throw new Error(`FFC ADP fetch failed: HTTP ${res.status}`);
  return res.json();
}

export function normalize(raw) {
  return (raw?.players ?? []).map((p) => ({
    name: p.name,
    position: p.position,
    team: p.team,
    adp: p.adp,
    timesDrafted: p.times_drafted,
    high: p.high,
    low: p.low,
    stdev: p.stdev,
    bye: p.bye,
  }));
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `node --test test/sources-ffc.test.js`
Expected: PASS, 5/5.

- [ ] **Step 6: Commit**

```bash
git add src/sources/ffc.js test/sources-ffc.test.js test/fixtures/ffc-adp.json
git commit -m "feat: Fantasy Football Calculator ADP source module"
```

---

### Task 6: Source registry

**Files:**
- Create: `src/sources/index.js`
- Test: `test/sources-index.test.js`

**Interfaces:**
- Consumes: `meta` from Tasks 2 and 5.
- Produces: `SOURCES` (array of modules), `sourcesProviding(capability) -> Array<module>`, `allCapabilities() -> string[]`.

- [ ] **Step 1: Write the failing test**

```js
// test/sources-index.test.js
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test test/sources-index.test.js`
Expected: FAIL with `ERR_MODULE_NOT_FOUND` for `src/sources/index.js`.

- [ ] **Step 3: Write minimal implementation**

```js
// src/sources/index.js
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test test/sources-index.test.js`
Expected: PASS, 4/4.

- [ ] **Step 5: Commit**

```bash
git add src/sources/index.js test/sources-index.test.js
git commit -m "feat: source registry with capability lookup"
```

---

### Task 7: Enrichment join

**Files:**
- Create: `src/enrich.js`
- Test: `test/enrich.test.js`

**Interfaces:**
- Consumes: `buildCrosswalk`, `buildAdpIndex`, `matchAdp`, `lookupByYahooKey` from Tasks 3–4.
- Produces: `enrichPlayers(yahooPlayers, {crosswalk, adpIndex, capabilities}) -> {players, stats}` where `stats` is `{total, matched: {adp, injury}}`.

- [ ] **Step 1: Write the failing test**

```js
// test/enrich.test.js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildCrosswalk, buildAdpIndex } from '../src/identity.js';
import { enrichPlayers } from '../src/enrich.js';

const CROSSWALK = buildCrosswalk([
  { yahooId: '26686', sleeperId: '1466', name: 'Travis Kelce', position: 'TE',
    team: 'KC', injuryStatus: null },
  { yahooId: '30977', sleeperId: '4034', name: 'Christian McCaffrey', position: 'RB',
    team: 'SF', injuryStatus: 'Questionable' },
]);
const ADP = buildAdpIndex([
  { name: 'Travis Kelce', position: 'TE', team: 'KC', adp: 40.2 },
]);

const YAHOO = [
  { player_key: '470.p.26686', name: { full: 'Travis Kelce' }, display_position: 'TE' },
  { player_key: '470.p.30977', name: { full: 'Christian McCaffrey' }, display_position: 'RB' },
  { player_key: '470.p.99999', name: { full: 'Unknown Guy' }, display_position: 'WR' },
];

test('enrichPlayers attaches ADP to players that match', () => {
  const { players } = enrichPlayers(YAHOO, {
    crosswalk: CROSSWALK, adpIndex: ADP, capabilities: ['adp'] });
  assert.equal(players.find((p) => p.name.full === 'Travis Kelce').adp, 40.2);
});

test('enrichPlayers attaches injury status from the crosswalk', () => {
  const { players } = enrichPlayers(YAHOO, {
    crosswalk: CROSSWALK, adpIndex: ADP, capabilities: ['injury'] });
  assert.equal(players.find((p) => p.name.full === 'Christian McCaffrey').injury,
    'Questionable');
});

test('enrichPlayers leaves unmatched players untouched rather than failing', () => {
  const { players } = enrichPlayers(YAHOO, {
    crosswalk: CROSSWALK, adpIndex: ADP, capabilities: ['adp', 'injury'] });
  const unknown = players.find((p) => p.name.full === 'Unknown Guy');
  assert.equal(unknown.adp, undefined);
  assert.equal(unknown.injury, undefined);
  assert.equal(unknown.name.full, 'Unknown Guy');
});

test('enrichPlayers only attaches the capabilities that were requested', () => {
  const { players } = enrichPlayers(YAHOO, {
    crosswalk: CROSSWALK, adpIndex: ADP, capabilities: ['injury'] });
  assert.equal(players.find((p) => p.name.full === 'Travis Kelce').adp, undefined);
});

test('enrichPlayers reports match rates so silent degradation is visible', () => {
  const { stats } = enrichPlayers(YAHOO, {
    crosswalk: CROSSWALK, adpIndex: ADP, capabilities: ['adp', 'injury'] });
  assert.equal(stats.total, 3);
  assert.equal(stats.matched.adp, 1);
  assert.equal(stats.matched.injury, 1);
});

test('enrichPlayers returns players unchanged when no capabilities are requested', () => {
  const { players } = enrichPlayers(YAHOO, {
    crosswalk: CROSSWALK, adpIndex: ADP, capabilities: [] });
  assert.deepEqual(players, YAHOO);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test test/enrich.test.js`
Expected: FAIL with `ERR_MODULE_NOT_FOUND` for `src/enrich.js`.

- [ ] **Step 3: Write minimal implementation**

```js
// src/enrich.js
import { lookupByYahooKey, matchAdp } from './identity.js';

/**
 * Decorate Yahoo players with external attributes.
 * Unmatched players pass through untouched: enrichment is additive and must
 * never break a command that worked without it.
 * `stats` exposes match counts so a silent drop in match rate is visible.
 */
export function enrichPlayers(players, { crosswalk, adpIndex, capabilities = [] } = {}) {
  const want = new Set(capabilities);
  const stats = { total: players.length, matched: { adp: 0, injury: 0 } };
  if (want.size === 0) return { players, stats };

  const enriched = players.map((p) => {
    const out = { ...p };
    const xw = lookupByYahooKey(crosswalk, p.player_key);

    if (want.has('injury') && xw?.injuryStatus) {
      out.injury = xw.injuryStatus;
      stats.matched.injury += 1;
    }
    if (want.has('adp')) {
      const hit = matchAdp(adpIndex, {
        name: p.name?.full ?? xw?.name,
        position: p.display_position ?? xw?.position,
        team: xw?.team,
      });
      if (hit) { out.adp = hit.adp; stats.matched.adp += 1; }
    }
    return out;
  });

  return { players: enriched, stats };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test test/enrich.test.js`
Expected: PASS, 6/6.

- [ ] **Step 5: Commit**

```bash
git add src/enrich.js test/enrich.test.js
git commit -m "feat: join external attributes onto Yahoo players"
```

---

### Task 8: CLI wiring — `--with`, `sync`, `sources`

**Files:**
- Modify: `src/cli.js` (add `parseWith`, `sync` and `sources` cases, `--with` on `players`/`roster`)
- Modify: `bin/tt.js` (route `sync`/`sources`, which need no Yahoo client)
- Test: `test/cli.test.js` (append)

**Interfaces:**
- Consumes: `enrichPlayers` (Task 7), `SOURCES`/`allCapabilities` (Task 6), cache (Task 1).
- Produces: `parseWith(flagValue) -> string[]`; new CLI commands `sync` and `sources`.

- [ ] **Step 1: Write the failing test**

```js
// append to test/cli.test.js
import { parseWith } from '../src/cli.js';
import { allCapabilities } from '../src/sources/index.js';

test('parseWith splits a comma-separated capability list', () => {
  assert.deepEqual(parseWith('adp,injury'), ['adp', 'injury']);
});

test('parseWith trims whitespace and drops empty entries', () => {
  assert.deepEqual(parseWith('adp , , injury '), ['adp', 'injury']);
});

test('parseWith returns an empty list when the flag is absent or bare', () => {
  assert.deepEqual(parseWith(undefined), []);
  assert.deepEqual(parseWith(true), []);
});

test('parseWith rejects a capability no source provides', () => {
  assert.throws(() => parseWith('nonsense'), /nonsense/);
});

test('every capability parseWith accepts is one a source actually provides', () => {
  for (const cap of allCapabilities()) assert.deepEqual(parseWith(cap), [cap]);
});

test('sources lists registered sources with their capabilities', async () => {
  const out = capture();
  const code = await runCommand({ command: 'sources', args: [], flags: {} }, { out });
  assert.equal(code, 0);
  assert.match(out.text(), /sleeper/);
  assert.match(out.text(), /ffc/);
  assert.match(out.text(), /adp/);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test test/cli.test.js`
Expected: FAIL with "does not provide an export named 'parseWith'".

- [ ] **Step 3: Write minimal implementation**

```js
// add to src/cli.js
import { SOURCES, allCapabilities, sourcesProviding } from './sources/index.js';
import { readCache, writeCache } from './cache.js';
import { buildCrosswalk, buildAdpIndex } from './identity.js';
import { enrichPlayers } from './enrich.js';

/** Parse `--with=adp,injury` into validated capability names. */
export function parseWith(value) {
  if (!value || value === true) return [];
  const known = new Set(allCapabilities());
  const caps = String(value).split(',').map((s) => s.trim()).filter(Boolean);
  for (const c of caps) {
    if (!known.has(c)) {
      throw new YahooApiError(
        `Unknown capability "${c}". Available: ${[...known].join(', ')}`);
    }
  }
  return caps;
}
```

Add a `sources` case to the `switch` in `runCommand`:

```js
      case 'sources': {
        const rows = SOURCES.map((s) => ({
          name: s.meta.name,
          provides: s.meta.provides.join(','),
          join: s.meta.joinKey,
          ttl: `${s.meta.ttlHours}h`,
          documented: s.meta.documented ? 'yes' : 'NO',
        }));
        emit(rows, [
          { key: 'name', label: 'SOURCE' },
          { key: 'provides', label: 'PROVIDES' },
          { key: 'join', label: 'JOIN' },
          { key: 'ttl', label: 'TTL' },
          { key: 'documented', label: 'DOCUMENTED' },
        ]);
        return 0;
      }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test test/cli.test.js`
Expected: PASS — the six new tests plus all existing ones.

- [ ] **Step 5: Add the `sync` command**

```js
      case 'sync': {
        const only = flags.source;
        for (const s of SOURCES) {
          if (only && s.meta.name !== only) continue;
          const cached = await readCache(s.meta.name, { ttlHours: s.meta.ttlHours });
          if (cached && !cached.stale && !flags.force) {
            out.write(`${s.meta.name}: fresh (cached)\n`);
            continue;
          }
          const records = s.normalize(await s.fetchRaw({ fetch: globalThis.fetch }));
          await writeCache(s.meta.name, records);
          out.write(`${s.meta.name}: ${records.length} records\n`);
        }
        return 0;
      }
```

- [ ] **Step 6: Wire `--with` into `players` and `roster`**

Replace the whole `players` case in `runCommand` with this version. Enrichment
happens on the raw player objects (before row mapping) because that is where
`player_key` still exists.

```js
      case 'players': {
        const key = await resolveLeagueKey(client, args[0]);
        const caps = parseWith(flags.with);
        const { league } = await client.get(buildPlayersResource(key, flags));
        let players = league.players ?? [];

        if (caps.length) {
          const sleeperCached = await readCache('sleeper', { ttlHours: 24 });
          const ffcCached = await readCache('ffc', { ttlHours: 12 });
          if (!sleeperCached || !ffcCached) {
            // Enrichment is additive: a cold cache must not fail the command.
            err.write('Enrichment cache is empty. Run: tt sync\n');
          } else {
            ({ players } = enrichPlayers(players, {
              crosswalk: buildCrosswalk(sleeperCached.data),
              adpIndex: buildAdpIndex(ffcCached.data),
              capabilities: caps,
            }));
          }
        }

        const rows = players.map((p) => ({
          name: p.name?.full,
          pos: p.display_position,
          team: p.editorial_team_abbr,
          bye: p.bye_weeks?.week,
          status: p.status ?? '',
          adp: p.adp,
          injury: p.injury,
        }));

        const columns = [
          { key: 'name', label: 'PLAYER' },
          { key: 'pos', label: 'POS' },
          { key: 'team', label: 'TEAM' },
          { key: 'bye', label: 'BYE' },
          { key: 'status', label: 'STATUS' },
        ];
        if (caps.includes('adp')) columns.push({ key: 'adp', label: 'ADP' });
        if (caps.includes('injury')) columns.push({ key: 'injury', label: 'INJ' });

        emit(rows, columns);
        return 0;
      }
```

Apply the same pattern to the `roster` case. Its row mapping differs, so here it
is in full:

```js
        const { team } = await client.get(buildRosterResource(teamKey, flags));
        const caps = parseWith(flags.with);
        let players = team.roster?.players ?? [];

        if (caps.length) {
          const sleeperCached = await readCache('sleeper', { ttlHours: 24 });
          const ffcCached = await readCache('ffc', { ttlHours: 12 });
          if (!sleeperCached || !ffcCached) {
            err.write('Enrichment cache is empty. Run: tt sync\n');
          } else {
            ({ players } = enrichPlayers(players, {
              crosswalk: buildCrosswalk(sleeperCached.data),
              adpIndex: buildAdpIndex(ffcCached.data),
              capabilities: caps,
            }));
          }
        }

        if (!flags?.json) out.write(`Week ${team.roster?.week ?? '?'}\n`);
        const rows = players.map((p) => ({
          name: p.name?.full,
          pos: p.display_position,
          team: p.editorial_team_abbr,
          status: p.status ?? '',
          adp: p.adp,
          injury: p.injury,
        }));
        const columns = [
          { key: 'name', label: 'PLAYER' },
          { key: 'pos', label: 'POS' },
          { key: 'team', label: 'TEAM' },
          { key: 'status', label: 'STATUS' },
        ];
        if (caps.includes('adp')) columns.push({ key: 'adp', label: 'ADP' });
        if (caps.includes('injury')) columns.push({ key: 'injury', label: 'INJ' });
        emit(rows, columns);
        return 0;
```

- [ ] **Step 7: Route the new commands in `bin/tt.js`**

`sync` and `sources` need no Yahoo client, so route them before the client is built:

```js
if (parsed.command === 'sources') process.exit(await runCommand(parsed, {}));
if (parsed.command === 'sync') process.exit(await runCommand(parsed, {}));
```

- [ ] **Step 8: Run the full suite**

Run: `npm test`
Expected: all tests pass, no new warnings.

- [ ] **Step 9: Verify against live data**

```bash
node bin/tt.js sync
node bin/tt.js sources
node bin/tt.js players --position=RB --status=A --count=10 --with=adp,injury
```

Expected: `sync` reports non-zero record counts for both sources; `players` shows ADP and injury columns populated for most rows.

- [ ] **Step 10: Commit**

```bash
git add src/cli.js bin/tt.js test/cli.test.js
git commit -m "feat: --with enrichment, tt sync and tt sources"
```

---

### Task 9: Documentation

**Files:**
- Modify: `README.md`, `docs/multi-source-enrichment-design.md`

- [ ] **Step 1: Update the README**

Add to the CLI section:

```sh
node bin/tt.js sync                    # refresh enrichment caches
node bin/tt.js sources                 # what's registered and how stale
node bin/tt.js players --position=RB --status=A --with=adp,injury
```

Note that `sync` must be run once before `--with` works, and that caches live in `~/.tokentouchdowns/cache/`.

- [ ] **Step 2: Record the live match rate in the design doc**

Run `tt sync` and the enrichment command, then add the observed ADP match rate to §4.1 of `docs/multi-source-enrichment-design.md` as the baseline future runs are compared against.

- [ ] **Step 3: Commit**

```bash
git add README.md docs/multi-source-enrichment-design.md
git commit -m "docs: enrichment commands and observed ADP match rate"
```

---

## Deferred (not in this plan)

- nflverse historical/weekly stats (spec §2) — needs in-season data to verify.
- Sleeper projections (spec §2, §9.1) — undocumented endpoint; scoring-variant mapping unresolved.
- Sleeper as a league provider — explicitly out of scope (spec §10).
