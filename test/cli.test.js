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
import { IMPLEMENTED_CAPABILITIES } from '../src/enrich.js';
import { readCache, writeCache } from '../src/cache.js';

const HOUR = 60 * 60 * 1000;

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

test('the implemented capability list is exactly what enrichment attaches', () => {
  // Pinned explicitly rather than derived. The previous version of this test
  // compared parseWith's output against the same set parseWith validated
  // from, so it passed no matter what either side said.
  assert.deepEqual(IMPLEMENTED_CAPABILITIES, ['adp', 'injury']);
  for (const cap of IMPLEMENTED_CAPABILITIES) assert.deepEqual(parseWith(cap), [cap]);
});

test('parseWith rejects a capability a source advertises but enrichment never attaches', () => {
  // 'depth' is in meta.provides, so it used to validate -- and then attach
  // nothing, add no column and print no footer, exiting 0 with output
  // identical to no --with at all.
  assert.ok(allCapabilities().includes('depth'), 'precondition: the registry still advertises depth');
  assert.throws(() => parseWith('depth'), (e) =>
    e instanceof UsageError && /depth/.test(e.message) && /adp, injury/.test(e.message));
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


// --- Final review: staleness, ADP variant, per-source requirements --------

test('players --with=adp still renders, and discloses that the cache is stale', async () => {
  // Spec §5: stale enrichment beats none, but serving a two-day-old ADP as
  // though it were today's is exactly the silent failure the spec forbids.
  const dir = tmpCacheDir();
  try {
    await writeCache('sleeper', SLEEPER_RECORDS, { dir, now: Date.now() - 31 * HOUR });
    await writeCache('ffc', FFC_RECORDS, { dir, now: Date.now() - 27 * HOUR });
    const out = capture(); const err = capture();
    const code = await runCommand(
      { command: 'players', args: [], flags: { position: 'QB', with: 'adp' } },
      { client: fakeClient, out, err, cacheDir: dir },
    );
    assert.equal(code, 0, 'a stale cache must still render the table');
    assert.match(out.text(), /Josh Allen/);
    assert.match(out.text(), /adp: 1 matched/);
    assert.match(err.text(), /stale/i);
    assert.match(err.text(), /ffc 27h/);
    assert.match(err.text(), /tt sync/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('players --with=adp says nothing about staleness when the cache is fresh', async () => {
  const dir = tmpCacheDir();
  try {
    await writeCache('sleeper', SLEEPER_RECORDS, { dir });
    await writeCache('ffc', FFC_RECORDS, { dir });
    const err = capture();
    await runCommand(
      { command: 'players', args: [], flags: { position: 'QB', with: 'adp' } },
      { client: fakeClient, out: capture(), err, cacheDir: dir },
    );
    assert.equal(err.text(), '');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('sources reports each cache\'s age and staleness, not just its TTL', async () => {
  const dir = tmpCacheDir();
  try {
    // ffc TTL is 12h; 27h old is stale. sleeper is deliberately never synced.
    await writeCache('ffc', { meta: { type: 'Half-PPR', teams: 10 }, records: FFC_RECORDS },
      { dir, now: Date.now() - 27 * HOUR });
    const out = capture();
    const code = await runCommand({ command: 'sources', args: [], flags: {} },
      { out, cacheDir: dir });
    assert.equal(code, 0);
    const lines = out.text().split('\n');
    assert.match(lines[0], /AGE/);
    assert.match(lines[0], /STALE/);
    const ffc = lines.find((l) => l.startsWith('ffc'));
    assert.match(ffc, /27h/, 'age comes from the cache file, not from meta');
    assert.match(ffc, /yes/, 'a 27h-old cache with a 12h TTL is stale');
    assert.match(ffc, /Half-PPR, 10-team/, 'which ADP board is cached');
    const sleeper = lines.find((l) => l.startsWith('sleeper'));
    assert.match(sleeper, /never/, 'a source with no cache reads as never synced');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('the adp footer names the scoring variant the numbers came from', async () => {
  const dir = tmpCacheDir();
  try {
    await writeCache('sleeper', SLEEPER_RECORDS, { dir });
    await writeCache('ffc',
      { meta: { type: 'Half-PPR', teams: 10 }, records: FFC_RECORDS }, { dir });
    const out = capture();
    await runCommand(
      { command: 'players', args: [], flags: { position: 'QB', with: 'adp' } },
      { client: fakeClient, out, cacheDir: dir },
    );
    assert.match(out.text(), /adp \[Half-PPR, 10-team\]: 1 matched, 0 ambiguous, 4 absent \(of 5\)/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('--with=injury works from the Sleeper cache alone, with no ADP cache at all', async () => {
  // Requiring both caches for any capability meant a valid 24h Sleeper cache
  // holding exactly what was asked for still reported "run tt sync".
  const dir = tmpCacheDir();
  try {
    await writeCache('sleeper', SLEEPER_RECORDS, { dir });
    const out = capture(); const err = capture();
    const code = await runCommand(
      { command: 'players', args: [], flags: { position: 'QB', with: 'injury' } },
      { client: fakeClient, out, err, cacheDir: dir },
    );
    assert.equal(code, 0);
    assert.equal(err.text(), '', 'no source providing injury is missing');
    const allen = out.text().split('\n').find((l) => l.includes('Josh Allen'));
    assert.match(allen, /Questionable/);
    assert.match(out.text(), /injury: 1 matched \(of 5\)/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('--with=adp names the specific source whose cache is missing', async () => {
  const dir = tmpCacheDir();
  try {
    await writeCache('sleeper', SLEEPER_RECORDS, { dir });
    const out = capture(); const err = capture();
    const code = await runCommand(
      { command: 'players', args: [], flags: { position: 'QB', with: 'adp' } },
      { client: fakeClient, out, err, cacheDir: dir },
    );
    assert.equal(code, 0);
    assert.match(err.text(), /ffc/);
    assert.doesNotMatch(err.text(), /sleeper/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('sync passes --scoring and --teams through to the ADP request', async () => {
  const dir = tmpCacheDir();
  try {
    let adpUrl;
    const fetchImpl = async (url) => {
      if (String(url).includes('fantasyfootballcalculator')) adpUrl = String(url);
      return fakeSyncFetch(url);
    };
    const code = await runCommand(
      { command: 'sync', args: [], flags: { source: 'ffc', scoring: 'half-ppr', teams: '10' } },
      { out: capture(), cacheDir: dir, fetch: fetchImpl },
    );
    assert.equal(code, 0);
    const u = new URL(adpUrl);
    assert.equal(u.pathname, '/api/v1/adp/half-ppr');
    assert.equal(u.searchParams.get('teams'), '10');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('sync rejects a scoring format FFC does not publish', async () => {
  const out = capture(); const err = capture();
  const code = await runCommand(
    { command: 'sync', args: [], flags: { scoring: 'superflex' } },
    { out, err, cacheDir: tmpCacheDir(), fetch: fakeSyncFetch },
  );
  assert.equal(code, 1);
  assert.match(err.text(), /superflex/);
  assert.match(err.text(), /half-ppr/);
});

test('sync --source=<unknown> is an error naming the real sources, not a silent no-op', async () => {
  const out = capture(); const err = capture();
  const code = await runCommand(
    { command: 'sync', args: [], flags: { source: 'sleper' } },
    { out, err, cacheDir: tmpCacheDir(), fetch: fakeSyncFetch },
  );
  assert.equal(code, 1);
  assert.match(err.text(), /sleper/);
  assert.match(err.text(), /sleeper, ffc/);
  assert.equal(out.text(), '');
});

test('a failing source does not stop the others, and sync exits non-zero', async () => {
  // Sleeper is first in SOURCES: an unhandled rejection there used to escape
  // as a raw stack and prevent FFC from syncing at all in that run.
  const dir = tmpCacheDir();
  try {
    const flaky = async (url) => {
      if (String(url).includes('sleeper')) throw new Error('getaddrinfo ENOTFOUND');
      return fakeSyncFetch(url);
    };
    const out = capture(); const err = capture();
    const code = await runCommand({ command: 'sync', args: [], flags: {} },
      { out, err, cacheDir: dir, fetch: flaky });
    assert.equal(code, 1, 'a failed source must be visible in the exit code');
    assert.match(err.text(), /sleeper: FAILED \(getaddrinfo ENOTFOUND\)/);
    assert.match(out.text(), /ffc: 1 records/, 'the other source still synced');
    assert.equal((await readCache('ffc', { dir, ttlHours: 12 })).data.records.length, 1);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('sync refuses to replace a populated cache with zero records', async () => {
  // If Sleeper renames yahoo_id, normalize returns []. Overwriting a working
  // 6,750-record crosswalk with that makes every later --with silently blank.
  const dir = tmpCacheDir();
  try {
    await writeCache('sleeper', SLEEPER_RECORDS, { dir, now: Date.now() - 48 * HOUR });
    const renamed = async (url) => {
      if (String(url).includes('sleeper')) {
        return { ok: true,
          json: async () => ({ 1: { player_id: '1', yahoo_id_v2: '30977', full_name: 'Josh Allen' } }) };
      }
      return fakeSyncFetch(url);
    };
    const out = capture(); const err = capture();
    const code = await runCommand({ command: 'sync', args: [], flags: {} },
      { out, err, cacheDir: dir, fetch: renamed });
    assert.equal(code, 1);
    assert.match(err.text(), /sleeper: REFUSED/);
    assert.doesNotMatch(out.text(), /sleeper: 0 records/);
    const kept = await readCache('sleeper', { dir, ttlHours: 24 });
    assert.equal(kept.data.length, 1, 'the working cache must survive');
    assert.equal(kept.data[0].yahooId, '30977');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('sync labels which ADP board it cached', async () => {
  const dir = tmpCacheDir();
  try {
    const withMeta = async (url) => {
      if (String(url).includes('fantasyfootballcalculator')) {
        return { ok: true, json: async () => ({
          meta: { type: 'Half-PPR', teams: 10, total_drafts: 3208 },
          players: [{ name: 'Josh Allen', position: 'QB', team: 'BUF', adp: 3.2 }],
        }) };
      }
      return fakeSyncFetch(url);
    };
    const out = capture();
    await runCommand({ command: 'sync', args: [], flags: { source: 'ffc' } },
      { out, cacheDir: dir, fetch: withMeta });
    assert.match(out.text(), /ffc: 1 records \[Half-PPR, 10-team\]/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// --- Task 1: league export --------------------------------------------

import { leagueConfig } from '../src/cli.js';

const leagueFixture = () => normalize(JSON.parse(readFileSync(
  new URL('./fixtures/league-settings.json', import.meta.url), 'utf8'))).league;

/** The scoring entry for a given stat_id, or undefined if none exists. */
const statEntry = (scoring, statId) => scoring.find((e) => e.statId === statId);

test('leagueConfig derives starter slots and scoring from league settings', () => {
  const cfg = leagueConfig(leagueFixture());

  assert.equal(cfg.numTeams, 4);
  assert.equal(cfg.maxTeams, 10);
  // Starters only -- BN and IR are not lineup slots.
  assert.deepEqual(cfg.rosterSlots,
    { QB: 1, RB: 2, WR: 2, TE: 1, 'W/R/T': 1, K: 1, DEF: 1 });
  // Scoring comes from stat_modifiers joined to stat_categories by stat_id,
  // not a constant and not a name (display_name is not unique -- see the
  // collision test below).
  assert.equal(statEntry(cfg.scoring, 11).value, 0.5); // Rec
  assert.equal(statEntry(cfg.scoring, 9).value, 0.1); // Rush Yds
  assert.equal(statEntry(cfg.scoring, 5).value, 4); // Pass TD
});

test('leagueConfig omits bench and IR from roster slots', () => {
  const cfg = leagueConfig(leagueFixture());
  assert.equal(cfg.rosterSlots.BN, undefined);
  assert.equal(cfg.rosterSlots.IR, undefined);
});

test('leagueConfig keeps both stats when two categories share a display name', () => {
  // Yahoo's own settings define BOTH stat_id 6 ("Interceptions", passing,
  // an individual QB's thrown picks, modifier -1) and stat_id 33
  // ("Interception", def_turnovers, a defense's takeaways, modifier +2)
  // under the identical display_name "Int". display_name is NOT a unique
  // key -- keying the export by stat_id (which Yahoo does treat as unique)
  // is what lets both survive, rather than one silently overwriting the
  // other and scoring every QB backwards.
  const cfg = leagueConfig(leagueFixture());
  const passingInt = statEntry(cfg.scoring, 6);
  const defensiveInt = statEntry(cfg.scoring, 33);
  assert.equal(passingInt.value, -1);
  assert.equal(passingInt.name, 'Int');
  assert.equal(defensiveInt.value, 2);
  assert.equal(defensiveInt.name, 'Int');
});

test('leagueConfig scoring entries never share a statId', () => {
  // The export's uniqueness invariant: a downstream reader keying off
  // statId must never find two entries claiming the same id.
  const cfg = leagueConfig(leagueFixture());
  const ids = cfg.scoring.map((e) => e.statId);
  assert.equal(ids.length, new Set(ids).size);
});

test('league export writes JSON a downstream tool can read', async () => {
  const out = capture();
  const client = { async get() {
    return normalize(JSON.parse(readFileSync(
      new URL('./fixtures/league-settings.json', import.meta.url), 'utf8'))); } };
  const code = await runCommand({ command: 'league', args: ['export'], flags: { json: true } },
    { client, out });
  assert.equal(code, 0);
  const cfg = JSON.parse(out.text());
  assert.equal(cfg.rosterSlots.RB, 2);
});

// --- Phase 2: draft board/pick, mock, lineup, playoff ---------------------

import { intFlag, round2, pct } from '../src/cli.js';

test('intFlag returns undefined when the flag is absent', () => {
  assert.equal(intFlag({}, 'teams'), undefined);
});

test('intFlag parses a numeric string value', () => {
  assert.equal(intFlag({ teams: '10' }, 'teams'), 10);
});

test('intFlag rejects a bare flag with no value', () => {
  assert.throws(() => intFlag({ teams: true }, 'teams'), UsageError);
  assert.throws(() => intFlag({ teams: true }, 'teams'), /--teams needs a value/);
});

test('intFlag rejects a non-integer value', () => {
  assert.throws(() => intFlag({ slot: 'abc' }, 'slot'), UsageError);
  assert.throws(() => intFlag({ slot: '2.5' }, 'slot'), UsageError);
});

test('round2 rounds to two decimal places and passes non-numbers through', () => {
  assert.equal(round2(122.4567), 122.46);
  assert.equal(round2(null), null);
  assert.equal(round2(undefined), undefined);
});

test('pct renders a probability as a whole-number percentage', () => {
  assert.equal(pct(0.874), '87%');
  assert.equal(pct(null), '-');
});

import { writeFile as fsWriteFile } from 'node:fs/promises';
import { AnalyticsError } from '../src/analytics.js';

/** A fake analytics client: `.run` returns a scripted response per
 * subcommand and records every call so tests can assert on the flags/stdin
 * `runCommand` sent it -- mirrors the fakeClient pattern above, but for the
 * Python bridge instead of the Yahoo API. */
function fakeAnalytics(responses = {}) {
  const calls = [];
  return {
    calls,
    async run(subcommand, opts = {}) {
      calls.push({ subcommand, ...opts });
      const entry = responses[subcommand];
      if (entry === undefined) throw new Error(`fakeAnalytics: no response scripted for "${subcommand}"`);
      if (entry instanceof Error) throw entry;
      return typeof entry === 'function' ? entry(opts) : entry;
    },
  };
}

const BOARD_RESPONSE = {
  season: 2026, teams: 4, slot: null, pick: null, next_pick: null,
  adp_source: 'ffc_adp_2025.json',
  players: [
    { player_id: 'rb1', name: 'RB One', position: 'RB', proj_points: 220.456, vor: 55.123, tier: 1, adp: 3.2 },
    { player_id: 'wr1', name: 'WR One', position: 'WR', proj_points: 180.1, vor: 30.0, tier: 2, adp: null },
  ],
};

test('draft board renders a ranked table with vor/tier/adp columns', async () => {
  const out = capture();
  const analytics = fakeAnalytics({ board: BOARD_RESPONSE });
  const code = await runCommand(
    { command: 'draft', args: ['board'], flags: { teams: '4' } },
    { client: fakeClient, out, analytics },
  );
  assert.equal(code, 0);
  const text = out.text();
  assert.match(text, /RB One/);
  assert.match(text, /VOR/);
  assert.match(text, /55\.12/);
  // adp_source footer, not the raw analytics_source field name
  assert.match(text, /ffc_adp_2025\.json/);
  assert.equal(analytics.calls.length, 1);
  assert.equal(analytics.calls[0].subcommand, 'board');
  assert.equal(analytics.calls[0].flags.teams, 4);
});

test('draft board passes --slot through and shows a survival column', async () => {
  const out = capture();
  const analytics = fakeAnalytics({
    board: { ...BOARD_RESPONSE, slot: 2, players: [
      { ...BOARD_RESPONSE.players[0], p_gone_by_next: 0.87 },
    ] },
  });
  const code = await runCommand(
    { command: 'draft', args: ['board'], flags: { slot: '2' } },
    { client: fakeClient, out, analytics },
  );
  assert.equal(code, 0);
  assert.equal(analytics.calls[0].flags.slot, 2);
  assert.match(out.text(), /P\(GONE\)/);
  assert.match(out.text(), /87%/);
});

test('draft board --json dumps the raw analytics payload', async () => {
  const out = capture();
  const analytics = fakeAnalytics({ board: BOARD_RESPONSE });
  await runCommand(
    { command: 'draft', args: ['board'], flags: { json: true } },
    { client: fakeClient, out, analytics },
  );
  const parsed = JSON.parse(out.text());
  assert.equal(parsed.adp_source, 'ffc_adp_2025.json');
  assert.equal(parsed.players.length, 2);
});

test('draft board surfaces a clean AnalyticsError message rather than a stack trace', async () => {
  const out = capture(); const err = capture();
  const analytics = fakeAnalytics({
    board: new AnalyticsError('League config not found at data/league.json. Run: tt league export --out=data/league.json'),
  });
  const code = await runCommand(
    { command: 'draft', args: ['board'], flags: {} },
    { client: fakeClient, out, err, analytics },
  );
  assert.notEqual(code, 0);
  assert.match(err.text(), /tt league export/);
});

test('draft pick requires a recognised subcommand', async () => {
  const out = capture(); const err = capture();
  const code = await runCommand(
    { command: 'draft', args: ['nonsense'], flags: {} },
    { client: fakeClient, out, err, analytics: fakeAnalytics({}) },
  );
  assert.notEqual(code, 0);
  assert.match(err.text(), /draft board/);
});

const PICK_RESPONSE = {
  season: 2026, teams: 4, slot: 2, round: 1, pick: 2, next_pick: 7, adp_source: null,
  recommendations: [
    { player_id: 'rb2', name: 'RB Two', position: 'RB', vor: 40.0, p_gone_by_next: 0.6, expected_loss: 24.0 },
  ],
};

test('draft pick reads --roster from a file and forwards it as stdin', async () => {
  const dir = tmpCacheDir();
  try {
    const rosterPath = path.join(dir, 'roster.json');
    await fsWriteFile(rosterPath, JSON.stringify([{ player_id: 'rb1', position: 'RB' }]));
    const out = capture();
    const analytics = fakeAnalytics({ pick: PICK_RESPONSE });
    const code = await runCommand(
      { command: 'draft', args: ['pick'], flags: { slot: '2', roster: rosterPath } },
      { client: fakeClient, out, analytics },
    );
    assert.equal(code, 0);
    assert.match(out.text(), /RB Two/);
    assert.match(out.text(), /Pick 2/);
    assert.deepEqual(analytics.calls[0].stdin, { roster: [{ player_id: 'rb1', position: 'RB' }] });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('draft pick with no --roster sends an empty roster, not a crash', async () => {
  const out = capture();
  const analytics = fakeAnalytics({ pick: PICK_RESPONSE });
  const code = await runCommand(
    { command: 'draft', args: ['pick'], flags: { slot: '2' } },
    { client: fakeClient, out, analytics },
  );
  assert.equal(code, 0);
  assert.deepEqual(analytics.calls[0].stdin, { roster: [] });
});

test('draft pick reports a clear error for a missing --roster file', async () => {
  const out = capture(); const err = capture();
  const code = await runCommand(
    { command: 'draft', args: ['pick'], flags: { slot: '2', roster: '/no/such/file.json' } },
    { client: fakeClient, out, err, analytics: fakeAnalytics({ pick: PICK_RESPONSE }) },
  );
  assert.notEqual(code, 0);
  assert.match(err.text(), /roster/i);
});

test('draft pick rejects a bare --roster flag with no path', async () => {
  const out = capture(); const err = capture();
  const code = await runCommand(
    { command: 'draft', args: ['pick'], flags: { slot: '2', roster: true } },
    { client: fakeClient, out, err, analytics: fakeAnalytics({ pick: PICK_RESPONSE }) },
  );
  assert.notEqual(code, 0);
  // Specifically the dedicated "no path given" message, not merely any
  // error mentioning --roster -- fs.readFile(true, ...) also rejects (it
  // isn't a valid path type), and that fallback's message happens to
  // contain the substring "--roster" too, so a looser assertion here would
  // pass even if the dedicated bare-flag check were deleted.
  assert.match(err.text(), /--roster needs a file path/);
});

const MOCK_RESPONSE = {
  season: 2026, teams: 4, slot: 2, rounds: 5, trials: 50, adp_source: null,
  note: 'Strategies are graded on this engine\'s own proj_points...',
  strategies: [
    { strategy: 'adp', trials: 50, mean_score: 1200.4, std_score: 30.2, sem_score: 4.3, ci95_low: 1190, ci95_high: 1210 },
    { strategy: 'vor_survival', trials: 50, mean_score: 1230.1, std_score: 28.0, sem_score: 4.0, ci95_low: 1220, ci95_high: 1240 },
  ],
};

test('mock renders the strategy comparison table with uncertainty', async () => {
  const out = capture();
  const analytics = fakeAnalytics({ mock: MOCK_RESPONSE });
  const code = await runCommand(
    { command: 'mock', args: [], flags: { trials: '50', strategy: 'adp,vor_survival' } },
    { client: fakeClient, out, analytics },
  );
  assert.equal(code, 0);
  const text = out.text();
  assert.match(text, /vor_survival/);
  assert.match(text, /1230\.1/);
  assert.equal(analytics.calls[0].flags.trials, 50);
  assert.equal(analytics.calls[0].flags.strategy, 'adp,vor_survival');
});

test('mock --strategy requires a value', async () => {
  const out = capture(); const err = capture();
  const code = await runCommand(
    { command: 'mock', args: [], flags: { strategy: true } },
    { client: fakeClient, out, err, analytics: fakeAnalytics({ mock: MOCK_RESPONSE }) },
  );
  assert.notEqual(code, 0);
  assert.match(err.text(), /--strategy/);
});

// --- lineup / playoff: identity resolution + analytics wiring -------------

const SLEEPER_WITH_GSIS = [
  { sleeperId: '1', yahooId: '30977', gsisId: '00-0034857', name: 'Josh Allen', position: 'QB', team: 'BUF' },
  { sleeperId: '2', yahooId: '30123', gsisId: '00-0036000', name: 'Rookie Guy', position: 'RB', team: 'SF' },
];

function rosterClientFor(players) {
  return {
    async get(resource) {
      if (resource.includes('users;use_login=1')) return fx('user-leagues');
      if (resource.endsWith('/teams')) return fx('league-teams');
      if (resource.endsWith('/roster') || resource.includes('/roster;')) {
        return { team: { roster: { week: 1, players } } };
      }
      throw new Error(`unexpected resource: ${resource}`);
    },
  };
}

const LINEUP_RESPONSE = {
  season: 2026, week: 1,
  lineup: [
    { player_id: '00-0034857', name: 'Josh Allen', position: 'QB', slot: 'QB', starter: true, empty: false, proj_points: 21.4 },
    { player_id: null, name: null, position: null, slot: 'RB', starter: true, empty: true, proj_points: 0.0 },
  ],
  unprojected_players: [],
};

test('lineup resolves the Yahoo roster through the crosswalk and reports the match rate', async () => {
  const dir = tmpCacheDir();
  try {
    await writeCache('sleeper', SLEEPER_WITH_GSIS, { dir });
    const players = [
      { player_key: '470.p.30977', name: { full: 'Josh Allen' }, display_position: 'QB' },
    ];
    const out = capture();
    const analytics = fakeAnalytics({ lineup: LINEUP_RESPONSE });
    const code = await runCommand(
      { command: 'lineup', args: [], flags: {} },
      { client: rosterClientFor(players), out, cacheDir: dir, analytics },
    );
    assert.equal(code, 0);
    const text = out.text();
    assert.match(text, /Josh Allen/);
    assert.match(text, /identity: 1 matched, 0 unresolved \(of 1\)/);
    assert.deepEqual(analytics.calls[0].stdin, {
      roster: [{ player_id: '00-0034857', name: 'Josh Allen', position: 'QB' }],
    });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('lineup names an unresolved roster player instead of silently dropping them', async () => {
  const dir = tmpCacheDir();
  try {
    await writeCache('sleeper', SLEEPER_WITH_GSIS, { dir });
    const players = [
      { player_key: '470.p.999999', name: { full: 'Mystery Backup' }, display_position: 'WR' },
    ];
    const out = capture();
    const analytics = fakeAnalytics({ lineup: { ...LINEUP_RESPONSE, unprojected_players: ['Mystery Backup'] } });
    const code = await runCommand(
      { command: 'lineup', args: [], flags: {} },
      { client: rosterClientFor(players), out, cacheDir: dir, analytics },
    );
    assert.equal(code, 0);
    const text = out.text();
    assert.match(text, /identity: 0 matched, 1 unresolved \(of 1\)/);
    assert.match(text, /Unresolved: Mystery Backup/);
    assert.match(text, /Mystery Backup/); // also named in the "no projection" footer
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

const NFLVERSE_FIXTURE = new URL('./fixtures/nflverse-players.json', import.meta.url).pathname;

test('lineup falls back to the nflverse name crosswalk when Sleeper has no gsis_id for a player', async () => {
  // A REAL, measured gap (see analytics/scripts/build_ffc_crosswalk.mjs's
  // own module docstring): Sleeper's yahoo_id -> gsis_id join alone
  // resolves only ~19% of a real Yahoo league's player pool, because
  // Sleeper's gsis_id field is null for exactly the star players a league
  // cares about most. A Sleeper record present but with gsisId: null
  // (Fallback Rookie below) must still resolve, via the SAME tested
  // buildAdpIndex/matchAdp matcher (src/identity.js) applied to nflverse's
  // own roster export -- the identical second-pass technique
  // build_ffc_crosswalk.mjs already uses for the ADP crosswalk.
  const dir = tmpCacheDir();
  try {
    await writeCache('sleeper', [
      { sleeperId: '9', yahooId: '30500', gsisId: null, name: 'Fallback Rookie', position: 'WR', team: 'SF' },
    ], { dir });
    const players = [
      { player_key: '470.p.30500', name: { full: 'Fallback Rookie' }, display_position: 'WR', editorial_team_abbr: 'SF' },
    ];
    const out = capture();
    const analytics = fakeAnalytics({ lineup: LINEUP_RESPONSE });
    const code = await runCommand(
      { command: 'lineup', args: [], flags: {} },
      { client: rosterClientFor(players), out, cacheDir: dir, analytics, nflverseRosterPath: NFLVERSE_FIXTURE },
    );
    assert.equal(code, 0);
    assert.match(out.text(), /identity: 1 matched, 0 unresolved \(of 1\)/);
    assert.deepEqual(analytics.calls[0].stdin, {
      roster: [{ player_id: '00-0039999', name: 'Fallback Rookie', position: 'WR' }],
    });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('lineup still names a player neither Sleeper nor the nflverse fallback can resolve', async () => {
  const dir = tmpCacheDir();
  try {
    await writeCache('sleeper', SLEEPER_WITH_GSIS, { dir });
    const players = [
      { player_key: '470.p.404', name: { full: 'Nobody Knows Me' }, display_position: 'WR', editorial_team_abbr: 'ZZ' },
    ];
    const out = capture();
    const analytics = fakeAnalytics({ lineup: { ...LINEUP_RESPONSE, unprojected_players: ['Nobody Knows Me'] } });
    const code = await runCommand(
      { command: 'lineup', args: [], flags: {} },
      { client: rosterClientFor(players), out, cacheDir: dir, analytics, nflverseRosterPath: NFLVERSE_FIXTURE },
    );
    assert.equal(code, 0);
    assert.match(out.text(), /identity: 0 matched, 1 unresolved \(of 1\)/);
    assert.match(out.text(), /Unresolved: Nobody Knows Me/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('lineup degrades gracefully when the nflverse roster export file is missing (Sleeper-only match)', async () => {
  const dir = tmpCacheDir();
  try {
    await writeCache('sleeper', SLEEPER_WITH_GSIS, { dir });
    const players = [{ player_key: '470.p.30977', name: { full: 'Josh Allen' }, display_position: 'QB' }];
    const out = capture();
    const analytics = fakeAnalytics({ lineup: LINEUP_RESPONSE });
    const code = await runCommand(
      { command: 'lineup', args: [], flags: {} },
      {
        client: rosterClientFor(players), out, cacheDir: dir, analytics,
        nflverseRosterPath: '/no/such/file/nflverse_players.json',
      },
    );
    assert.equal(code, 0);
    assert.match(out.text(), /identity: 1 matched, 0 unresolved \(of 1\)/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('lineup surfaces AnalyticsError cleanly', async () => {
  const dir = tmpCacheDir();
  try {
    const out = capture(); const err = capture();
    const analytics = fakeAnalytics({ lineup: new AnalyticsError('No nflverse parquet files found') });
    const code = await runCommand(
      { command: 'lineup', args: [], flags: {} },
      { client: rosterClientFor([]), out, err, cacheDir: dir, analytics },
    );
    assert.notEqual(code, 0);
    assert.match(err.text(), /parquet/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('lineup propagates an expired Yahoo session the same way every other command does', async () => {
  const out = capture(); const err = capture();
  const dead = { async get() { throw new SessionExpiredError('401'); } };
  const code = await runCommand(
    { command: 'lineup', args: [], flags: {} },
    { client: dead, out, err, interactive: false, analytics: fakeAnalytics({}) },
  );
  assert.notEqual(code, 0);
  assert.match(err.text(), /tt login/);
});

const PLAYOFF_RESPONSE = {
  season: 2026, week: 16,
  lineup: [
    { player_id: '00-0034857', name: 'Josh Allen', position: 'QB', slot: 'QB', starter: true, empty: false, proj_points: 21.4 },
  ],
  win_probability: 0.63,
  expected_points_lineup_win_probability: 0.58,
  expected_points_lineup_points: 140.2,
  playoff_lineup_points: 138.9,
  unprojected_players: [],
  opponent_unprojected_players: [],
};

function playoffClient(myPlayers, oppPlayers, oppName = 'Rival Team') {
  return {
    async get(resource) {
      if (resource.includes('users;use_login=1')) return fx('user-leagues');
      if (resource.endsWith('/teams')) return fx('league-teams');
      if (resource.endsWith('/matchups')) {
        return {
          team: {
            matchups: [
              {
                week: '16', status: 'preevent',
                teams: [
                  { team_key: '470.l.1433971.t.4', name: 'Token Maxxing Touchdowns', is_owned_by_current_login: 1 },
                  { team_key: '470.l.1433971.t.7', name: oppName, is_owned_by_current_login: 0 },
                ],
              },
            ],
          },
        };
      }
      if (resource.startsWith('team/470.l.1433971.t.4/roster')) {
        return { team: { roster: { week: 16, players: myPlayers } } };
      }
      if (resource.startsWith('team/470.l.1433971.t.7/roster')) {
        return { team: { roster: { week: 16, players: oppPlayers } } };
      }
      throw new Error(`unexpected resource: ${resource}`);
    },
  };
}

test('playoff defaults to the current matchup opponent and reports win probability', async () => {
  const dir = tmpCacheDir();
  try {
    await writeCache('sleeper', SLEEPER_WITH_GSIS, { dir });
    const mine = [{ player_key: '470.p.30977', name: { full: 'Josh Allen' }, display_position: 'QB' }];
    const theirs = [{ player_key: '470.p.30123', name: { full: 'Rookie Guy' }, display_position: 'RB' }];
    const out = capture();
    const analytics = fakeAnalytics({ playoff: PLAYOFF_RESPONSE });
    const code = await runCommand(
      { command: 'playoff', args: [], flags: {} },
      { client: playoffClient(mine, theirs), out, cacheDir: dir, analytics },
    );
    assert.equal(code, 0);
    const text = out.text();
    assert.match(text, /Rival Team/);
    assert.match(text, /63%/);
    assert.deepEqual(analytics.calls[0].stdin, {
      roster: [{ player_id: '00-0034857', name: 'Josh Allen', position: 'QB' }],
      opponent_roster: [{ player_id: '00-0036000', name: 'Rookie Guy', position: 'RB' }],
    });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('playoff --opponent selects a specific team by name', async () => {
  const dir = tmpCacheDir();
  try {
    await writeCache('sleeper', SLEEPER_WITH_GSIS, { dir });
    const mine = [{ player_key: '470.p.30977', name: { full: 'Josh Allen' }, display_position: 'QB' }];
    const theirs = [];
    const out = capture();
    const analytics = fakeAnalytics({ playoff: PLAYOFF_RESPONSE });
    const code = await runCommand(
      { command: 'playoff', args: [], flags: { opponent: 'rival' } },
      { client: playoffClient(mine, theirs), out, cacheDir: dir, analytics },
    );
    assert.equal(code, 0);
    assert.match(out.text(), /Rival Team/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('playoff reports clearly when --opponent matches no one in the matchup', async () => {
  const dir = tmpCacheDir();
  try {
    const out = capture(); const err = capture();
    const code = await runCommand(
      { command: 'playoff', args: [], flags: { opponent: 'nobody-by-this-name' } },
      { client: playoffClient([], []), out, err, cacheDir: dir, analytics: fakeAnalytics({ playoff: PLAYOFF_RESPONSE }) },
    );
    assert.notEqual(code, 0);
    assert.match(err.text(), /nobody-by-this-name/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
