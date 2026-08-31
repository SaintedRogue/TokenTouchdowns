import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseArgs, formatTable } from '../src/cli.js';

test('parseArgs extracts the command and its positional arguments', () => {
  assert.deepEqual(parseArgs(['teams', '470.l.1433971']),
    { command: 'teams', args: ['470.l.1433971'], flags: {} });
});

test('parseArgs collects long flags separately from positionals', () => {
  const { command, args, flags } = parseArgs(['roster', '470.l.1.t.4', '--json']);
  assert.equal(command, 'roster');
  assert.deepEqual(args, ['470.l.1.t.4']);
  assert.deepEqual(flags, { json: true });
});

test('parseArgs reads --key=value flags', () => {
  assert.deepEqual(parseArgs(['players', '--count=10']).flags, { count: '10' });
});

test('parseArgs defaults to help when given no command', () => {
  assert.equal(parseArgs([]).command, 'help');
});

test('parseArgs treats -h and --help as the help command', () => {
  assert.equal(parseArgs(['--help']).command, 'help');
  assert.equal(parseArgs(['-h']).command, 'help');
});

test('formatTable aligns columns to their widest cell', () => {
  const out = formatTable(
    [{ k: 'a', name: 'short' }, { k: 'bbbb', name: 'much longer name' }],
    [{ key: 'k', label: 'KEY' }, { key: 'name', label: 'NAME' }],
  );
  const lines = out.split('\n');
  assert.equal(lines[0], 'KEY   NAME');
  assert.equal(lines[1], 'a     short');
  assert.equal(lines[2], 'bbbb  much longer name');
});

test('formatTable renders missing values as a dash rather than undefined', () => {
  const out = formatTable([{ k: 'a' }], [{ key: 'k', label: 'K' }, { key: 'missing', label: 'M' }]);
  assert.match(out, /a\s+-/);
  assert.doesNotMatch(out, /undefined/);
});

test('formatTable returns a friendly message for an empty result set', () => {
  assert.equal(formatTable([], [{ key: 'k', label: 'K' }]), '(none)');
});

import { readFileSync } from 'node:fs';
import { runCommand } from '../src/cli.js';
import { normalize } from '../src/normalize.js';
import { SessionExpiredError } from '../src/client.js';

const fx = (n) => normalize(JSON.parse(
  readFileSync(new URL(`./fixtures/${n}.json`, import.meta.url), 'utf8')));

// Fake client backed by real captured responses, keyed by resource shape.
const fakeClient = {
  async get(resource) {
    if (resource.includes('users;use_login=1')) return fx('user-leagues');
    if (resource.endsWith('/teams')) return fx('league-teams');
    if (resource.endsWith('/standings')) return fx('league-standings');
    if (resource.endsWith('/roster')) return fx('team-roster');
    if (resource.endsWith('/matchups')) return fx('team-matchups');
    if (resource.includes('/players')) return fx('league-players-qb');
    throw new Error(`unexpected resource: ${resource}`);
  },
};
const capture = () => { const l = []; return { write: (s) => l.push(s), text: () => l.join('') }; };

test('leagues lists the user\'s leagues with keys and names', async () => {
  const out = capture();
  const code = await runCommand({ command: 'leagues', args: [], flags: {} },
    { client: fakeClient, out });
  assert.equal(code, 0);
  assert.match(out.text(), /470\.l\.1433971/);
  assert.match(out.text(), /Definitely Not Bots/);
});

test('teams marks the team owned by the logged-in user', async () => {
  const out = capture();
  await runCommand({ command: 'teams', args: ['470.l.1433971'], flags: {} },
    { client: fakeClient, out });
  const text = out.text();
  assert.match(text, /Token Maxxing Touchdowns/);
  // The user's own team must be visually distinguishable from the other three.
  const mineLine = text.split('\n').find((l) => l.includes('Token Maxxing Touchdowns'));
  assert.match(mineLine, /\*/, 'own team should be marked');
});

test('teams resolves the league from the user\'s leagues when omitted', async () => {
  const out = capture();
  const code = await runCommand({ command: 'teams', args: [], flags: {} },
    { client: fakeClient, out });
  assert.equal(code, 0);
  assert.match(out.text(), /Token Maxxing Touchdowns/);
});

test('--json emits parseable JSON instead of a table', async () => {
  const out = capture();
  await runCommand({ command: 'teams', args: ['470.l.1433971'], flags: { json: true } },
    { client: fakeClient, out });
  const parsed = JSON.parse(out.text());
  assert.equal(parsed.length, 4);
  assert.ok(parsed[0].team_key);
});

test('an expired session exits non-zero with an actionable message', async () => {
  const out = capture(); const err = capture();
  const dead = { async get() { throw new SessionExpiredError('401'); } };
  const code = await runCommand({ command: 'leagues', args: [], flags: {} },
    { client: dead, out, err, interactive: false });
  assert.notEqual(code, 0);
  assert.match(err.text(), /tt login/);
});

test('an unknown command exits non-zero and shows usage', async () => {
  const out = capture(); const err = capture();
  const code = await runCommand({ command: 'nope', args: [], flags: {} },
    { client: fakeClient, out, err });
  assert.notEqual(code, 0);
  assert.match(err.text(), /Unknown command/);
});

test('help lists the available commands and exits zero', async () => {
  const out = capture();
  const code = await runCommand({ command: 'help', args: [], flags: {} }, { out });
  assert.equal(code, 0);
  for (const c of ['login', 'leagues', 'teams', 'roster', 'standings']) {
    assert.match(out.text(), new RegExp(c));
  }
});

test('formatTable leaves an empty string blank, reserving the dash for absent keys', () => {
  // A marker column uses '' to mean "not marked" -- rendering that as '-' is noise.
  const out = formatTable(
    [{ mark: '*', name: 'mine' }, { mark: '', name: 'theirs' }],
    [{ key: 'mark', label: '' }, { key: 'name', label: 'NAME' }],
  );
  const theirs = out.split('\n').find((l) => l.includes('theirs'));
  assert.doesNotMatch(theirs, /-/, 'blank marker should stay blank');
});

import { buildPlayersResource } from '../src/cli.js';

test('buildPlayersResource emits matrix params Yahoo understands', () => {
  const r = buildPlayersResource('470.l.1', { position: 'QB', status: 'A', count: '5' });
  assert.ok(r.startsWith('league/470.l.1/players;'), r);
  assert.match(r, /;position=QB/);
  assert.match(r, /;status=A/);
  assert.match(r, /;count=5/);
});

test('buildPlayersResource always paginates so a request cannot run away', () => {
  const r = buildPlayersResource('470.l.1', {});
  assert.match(r, /;start=0/);
  assert.match(r, /;count=\d+/);
});

test('buildPlayersResource passes a search term through', () => {
  assert.match(buildPlayersResource('470.l.1', { search: 'kelce' }), /;search=kelce/);
});

test('buildPlayersResource ignores flags that are not player filters', () => {
  const r = buildPlayersResource('470.l.1', { json: true, position: 'WR' });
  assert.doesNotMatch(r, /json/);
  assert.match(r, /;position=WR/);
});

test('players lists available players with position and team', async () => {
  const out = capture();
  const code = await runCommand({ command: 'players', args: [], flags: { position: 'QB' } },
    { client: fakeClient, out });
  assert.equal(code, 0);
  const text = out.text();
  assert.match(text, /Josh Allen/);
  assert.match(text, /QB/);
});

test('matchup shows the current week with both teams', async () => {
  const out = capture();
  const code = await runCommand({ command: 'matchup', args: [], flags: {} },
    { client: fakeClient, out });
  assert.equal(code, 0);
  const text = out.text();
  assert.match(text, /Token Maxxing Touchdowns/);
  assert.match(text, /Week 1/);
});

test('matchup accepts an explicit week number', async () => {
  const out = capture();
  await runCommand({ command: 'matchup', args: ['3'], flags: {} }, { client: fakeClient, out });
  assert.match(out.text(), /Week 3/);
});

test('matchup reports clearly when the requested week has no matchup', async () => {
  const out = capture(); const err = capture();
  const code = await runCommand({ command: 'matchup', args: ['99'], flags: {} },
    { client: fakeClient, out, err });
  assert.notEqual(code, 0);
  assert.match(err.text(), /week 99/i);
});

test('buildPlayersResource sorts by overall rank by default', () => {
  // Yahoo's unsorted order is arbitrary -- browsing a pool, you want the best
  // available first, not a fullback ahead of Derrick Henry.
  assert.match(buildPlayersResource('470.l.1', {}), /;sort=OR/);
});

test('buildPlayersResource lets an explicit sort override the default', () => {
  const r = buildPlayersResource('470.l.1', { sort: 'AR' });
  assert.match(r, /;sort=AR/);
  assert.doesNotMatch(r, /;sort=OR/);
});

import { buildTransactionsResource, formatTimestamp } from '../src/cli.js';

const synthetic = () => normalize(JSON.parse(readFileSync(
  new URL('./fixtures/league-transactions-SYNTHETIC.json', import.meta.url), 'utf8')));

test('formatTimestamp renders Yahoo epoch seconds as a UTC date', () => {
  // UTC, not local: the same transaction must not render differently by machine.
  assert.equal(formatTimestamp('1757980800'), '2025-09-16');
});

test('formatTimestamp renders a dash for a missing timestamp', () => {
  assert.equal(formatTimestamp(undefined), '-');
});

test('buildTransactionsResource applies type and count filters', () => {
  const r = buildTransactionsResource('470.l.1', { type: 'add,drop', count: '10' });
  assert.ok(r.startsWith('league/470.l.1/transactions'), r);
  assert.match(r, /;types=add,drop/);
  assert.match(r, /;count=10/);
});

test('buildTransactionsResource requests no filters when none are given', () => {
  assert.equal(buildTransactionsResource('470.l.1', {}), 'league/470.l.1/transactions;count=25');
});

test('transactions renders one row per player movement', async () => {
  const out = capture();
  const client = { async get() { return synthetic(); } };
  const code = await runCommand({ command: 'transactions', args: ['470.l.1433971'], flags: {} },
    { client, out });
  assert.equal(code, 0);
  const text = out.text();
  // The add/drop transaction moves two players; both must appear.
  assert.match(text, /Travis Kelce/);
  assert.match(text, /Dallas Goedert/);
  assert.match(text, /Jahmyr Gibbs/);
  assert.match(text, /2025-09-16/);
});

test('transactions shows where each player came from and went to', async () => {
  const out = capture();
  const client = { async get() { return synthetic(); } };
  await runCommand({ command: 'transactions', args: ['470.l.1433971'], flags: {} },
    { client, out });
  const kelce = out.text().split('\n').find((l) => l.includes('Travis Kelce'));
  assert.match(kelce, /freeagents/);
  assert.match(kelce, /Token Maxxing Touchdowns/);
  const goedert = out.text().split('\n').find((l) => l.includes('Dallas Goedert'));
  assert.match(goedert, /waivers/);
});

test('transactions reports (none) for a league with no transactions', async () => {
  const out = capture();
  const client = { async get() { return fx('league-transactions'); } };
  const code = await runCommand({ command: 'transactions', args: ['470.l.1433971'], flags: {} },
    { client, out });
  assert.equal(code, 0);
  assert.match(out.text(), /\(none\)/);
});

import { buildRosterResource } from '../src/cli.js';

test('buildRosterResource requests the current roster when no week is given', () => {
  assert.equal(buildRosterResource('470.l.1.t.4', {}), 'team/470.l.1.t.4/roster');
});

test('buildRosterResource appends the week as a matrix parameter', () => {
  assert.equal(buildRosterResource('470.l.1.t.4', { week: '3' }), 'team/470.l.1.t.4/roster;week=3');
});

test('buildRosterResource ignores --week used as a bare flag', () => {
  // `--week` with no value parses as true; it must not become ";week=true".
  assert.equal(buildRosterResource('470.l.1.t.4', { week: true }), 'team/470.l.1.t.4/roster');
});

test('roster requests the week the user asked for', async () => {
  let seen;
  const client = {
    async get(resource) {
      seen = resource;
      if (resource.endsWith('/teams')) return fx('league-teams');
      if (resource.includes('users;use_login=1')) return fx('user-leagues');
      return fx('team-roster');
    },
  };
  await runCommand({ command: 'roster', args: ['470.l.1433971.t.4'], flags: { week: '3' } },
    { client, out: capture() });
  assert.match(seen, /;week=3$/);
});

test('roster labels which week it is showing', async () => {
  const out = capture();
  await runCommand({ command: 'roster', args: ['470.l.1433971.t.4'], flags: {} },
    { client: fakeClient, out });
  assert.match(out.text(), /Week 1/);
});

// --- Task 8: --with enrichment, sync, sources -----------------------------

import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { parseWith, UsageError } from '../src/cli.js';
import { allCapabilities } from '../src/sources/index.js';
import { writeCache } from '../src/cache.js';

const tmpCacheDir = () => mkdtempSync(path.join(tmpdir(), 'tt-cli-cache-'));

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

test('parseWith throws a UsageError, not a Yahoo-side error, for bad input', () => {
  // A local typo in --with is the user's mistake, not Yahoo's -- it must not
  // be reported (or exit-coded) as a Yahoo API failure.
  assert.throws(() => parseWith('nonsense'), (e) => e instanceof UsageError);
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

test('an invalid --with exits with the usage error code, not the Yahoo error code', async () => {
  const out = capture(); const err = capture();
  const code = await runCommand(
    { command: 'players', args: [], flags: { with: 'nonsense' } },
    { client: fakeClient, out, err },
  );
  assert.equal(code, 1);
  assert.match(err.text(), /nonsense/);
  assert.doesNotMatch(err.text(), /Yahoo API error/);
});

// Fake fetch for `sync`: keyed by URL substring so both source modules'
// fetchRaw implementations (which build different URLs) can share one stub.
const fakeSyncFetch = async (url) => {
  const u = String(url);
  if (u.includes('sleeper')) {
    return {
      ok: true,
      json: async () => ({
        1: { player_id: '1', yahoo_id: '30977', full_name: 'Josh Allen',
             position: 'QB', team: 'BUF', injury_status: 'Questionable' },
        2: { player_id: '2', yahoo_id: null, full_name: 'No Yahoo Id' },
      }),
    };
  }
  if (u.includes('fantasyfootballcalculator')) {
    return {
      ok: true,
      json: async () => ({ players: [{ name: 'Josh Allen', position: 'QB', team: 'BUF', adp: 3.2 }] }),
    };
  }
  throw new Error(`fakeSyncFetch: unexpected url ${u}`);
};

test('sync fetches every source and reports a record count', async () => {
  const dir = tmpCacheDir();
  try {
    const out = capture();
    const code = await runCommand({ command: 'sync', args: [], flags: {} },
      { out, cacheDir: dir, fetch: fakeSyncFetch });
    assert.equal(code, 0);
    // sleeper's second record has no yahoo_id and is dropped by normalize.
    assert.match(out.text(), /sleeper: 1 records/);
    assert.match(out.text(), /ffc: 1 records/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('sync writes normalized records to the cache for later reads', async () => {
  const dir = tmpCacheDir();
  try {
    await runCommand({ command: 'sync', args: [], flags: {} },
      { out: capture(), cacheDir: dir, fetch: fakeSyncFetch });
    const { readCache } = await import('../src/cache.js');
    const sleeper = await readCache('sleeper', { dir, ttlHours: 24 });
    assert.equal(sleeper.data.length, 1);
    assert.equal(sleeper.data[0].yahooId, '30977');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('sync skips a source that is already fresh in the cache', async () => {
  const dir = tmpCacheDir();
  try {
    await writeCache('sleeper', [{ yahooId: '1' }], { dir });
    await writeCache('ffc', [{ name: 'x' }], { dir });
    const out = capture();
    const code = await runCommand({ command: 'sync', args: [], flags: {} },
      { out, cacheDir: dir, fetch: fakeSyncFetch });
    assert.equal(code, 0);
    assert.match(out.text(), /sleeper: fresh \(cached\)/);
    assert.match(out.text(), /ffc: fresh \(cached\)/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('sync --force refetches even when the cache is fresh', async () => {
  const dir = tmpCacheDir();
  try {
    await writeCache('sleeper', [{ yahooId: '1' }], { dir });
    await writeCache('ffc', [{ name: 'x' }], { dir });
    const out = capture();
    const code = await runCommand({ command: 'sync', args: [], flags: { force: true } },
      { out, cacheDir: dir, fetch: fakeSyncFetch });
    assert.equal(code, 0);
    assert.match(out.text(), /sleeper: 1 records/);
    assert.match(out.text(), /ffc: 1 records/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('sync --source limits the sync to one named source', async () => {
  const dir = tmpCacheDir();
  try {
    const out = capture();
    const code = await runCommand({ command: 'sync', args: [], flags: { source: 'ffc' } },
      { out, cacheDir: dir, fetch: fakeSyncFetch });
    assert.equal(code, 0);
    assert.match(out.text(), /ffc: 1 records/);
    assert.doesNotMatch(out.text(), /sleeper/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

const SLEEPER_RECORDS = [
  { sleeperId: '1', yahooId: '30977', name: 'Josh Allen', position: 'QB',
    team: 'BUF', injuryStatus: 'Questionable' },
];
const FFC_RECORDS = [
  { name: 'Josh Allen', position: 'QB', team: 'BUF', adp: 3.2 },
];

test('players --with=adp,injury enriches matching rows and prints a match-rate footer', async () => {
  const dir = tmpCacheDir();
  try {
    await writeCache('sleeper', SLEEPER_RECORDS, { dir });
    await writeCache('ffc', FFC_RECORDS, { dir });
    const out = capture();
    const code = await runCommand(
      { command: 'players', args: [], flags: { position: 'QB', with: 'adp,injury' } },
      { client: fakeClient, out, cacheDir: dir },
    );
    assert.equal(code, 0);
    const text = out.text();
    const allenLine = text.split('\n').find((l) => l.includes('Josh Allen'));
    assert.match(allenLine, /3\.2/);
    assert.match(allenLine, /Questionable/);
    // 5 QBs in the fixture, only Josh Allen matches ADP.
    assert.match(text, /adp: 1 matched, 0 ambiguous, 4 absent \(of 5\)/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('players --with does not print the footer under --json', async () => {
  const dir = tmpCacheDir();
  try {
    await writeCache('sleeper', SLEEPER_RECORDS, { dir });
    await writeCache('ffc', FFC_RECORDS, { dir });
    const out = capture();
    await runCommand(
      { command: 'players', args: [], flags: { position: 'QB', with: 'adp', json: true } },
      { client: fakeClient, out, cacheDir: dir },
    );
    // Machine-readable output must stay pure JSON -- no trailing prose line.
    assert.doesNotThrow(() => JSON.parse(out.text()));
    assert.doesNotMatch(out.text(), /matched/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('players --with degrades to an unenriched table when the cache is empty', async () => {
  const dir = tmpCacheDir(); // deliberately left empty
  try {
    const out = capture(); const err = capture();
    const code = await runCommand(
      { command: 'players', args: [], flags: { position: 'QB', with: 'adp,injury' } },
      { client: fakeClient, out, err, cacheDir: dir },
    );
    assert.equal(code, 0, 'a cold cache must not fail a command that worked without --with');
    assert.match(out.text(), /Josh Allen/);
    assert.match(err.text(), /tt sync/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('players without --with is unaffected by enrichment machinery', async () => {
  const dir = tmpCacheDir();
  try {
    const out = capture();
    const code = await runCommand(
      { command: 'players', args: [], flags: { position: 'QB' } },
      { client: fakeClient, out, cacheDir: dir },
    );
    assert.equal(code, 0);
    assert.doesNotMatch(out.text(), /ADP/);
    assert.doesNotMatch(out.text(), /INJ/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('players --json without --with never leaks adp/injury keys into rows', async () => {
  // The row-mapping always sets `adp`/`injury` from the player object, which
  // is `undefined` when --with was not requested; JSON.stringify happens to
  // drop undefined-valued keys, but that is worth pinning explicitly rather
  // than relying on inspection to notice it stays true.
  const dir = tmpCacheDir();
  try {
    const out = capture();
    const code = await runCommand(
      { command: 'players', args: [], flags: { position: 'QB', json: true } },
      { client: fakeClient, out, cacheDir: dir },
    );
    assert.equal(code, 0);
    const rows = JSON.parse(out.text());
    assert.equal('adp' in rows[0], false);
    assert.equal('injury' in rows[0], false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('roster --with=adp,injury enriches roster rows', async () => {
  // The team-roster fixture's players array is empty (a real captured
  // response with no roster set yet), so this test supplies its own roster
  // with one real player instead.
  const dir = tmpCacheDir();
  try {
    await writeCache('sleeper', [
      { sleeperId: '1', yahooId: '30977', name: 'Josh Allen', position: 'QB',
        team: 'BUF', injuryStatus: 'Out' },
    ], { dir });
    await writeCache('ffc', [
      { name: 'Josh Allen', position: 'QB', team: 'BUF', adp: 7.5 },
    ], { dir });

    const rosterClient = {
      async get(resource) {
        if (resource.endsWith('/roster')) {
          return { team: { roster: { week: 1, players: [
            { player_key: '470.p.30977', name: { full: 'Josh Allen' },
              display_position: 'QB', editorial_team_abbr: 'Buf' },
          ] } } };
        }
        throw new Error(`unexpected resource: ${resource}`);
      },
    };

    const out = capture();
    const code = await runCommand(
      { command: 'roster', args: ['470.l.1433971.t.4'], flags: { with: 'adp,injury' } },
      { client: rosterClient, out, cacheDir: dir },
    );
    assert.equal(code, 0);
    const line = out.text().split('\n').find((l) => l.includes('Josh Allen'));
    assert.match(line, /7\.5/);
    assert.match(line, /Out/);
    // The roster footer is a separate copy of the same logic as players' --
    // without an assertion here it can drift silently, since deleting the
    // whole block leaves every other test in the file passing.
    assert.match(out.text(), /adp: 1 matched, 0 ambiguous, 0 absent \(of 1\)/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('players --with=adp degrades to an unenriched table on a corrupt cache entry', async () => {
  // Cache entries carry no schema version and outlive an upgrade: a source's
  // normalize() shape can change while an old cache file still parses fine.
  // buildCrosswalk/buildAdpIndex iterate `data` with `for...of`, which throws
  // a TypeError on a non-array payload -- that must degrade the same way a
  // cold cache does, not escape as an unhandled rejection.
  const dir = tmpCacheDir();
  try {
    await writeCache('sleeper', { not: 'an array' }, { dir });
    await writeCache('ffc', { not: 'an array' }, { dir });
    const out = capture(); const err = capture();
    const code = await runCommand(
      { command: 'players', args: [], flags: { position: 'QB', with: 'adp' } },
      { client: fakeClient, out, err, cacheDir: dir },
    );
    assert.equal(code, 0, 'a corrupt cache must not fail a command that worked without --with');
    assert.match(out.text(), /Josh Allen/);
    assert.match(err.text(), /corrupt/i);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
