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

test('leagueConfig derives starter slots and scoring from league settings', () => {
  const league = normalize(JSON.parse(readFileSync(
    new URL('./fixtures/league-settings.json', import.meta.url), 'utf8'))).league;
  const cfg = leagueConfig(league);

  assert.equal(cfg.numTeams, 4);
  assert.equal(cfg.maxTeams, 10);
  // Starters only -- BN and IR are not lineup slots.
  assert.deepEqual(cfg.rosterSlots,
    { QB: 1, RB: 2, WR: 2, TE: 1, 'W/R/T': 1, K: 1, DEF: 1 });
  // Scoring comes from stat_modifiers joined to stat_categories, not a constant.
  assert.equal(cfg.scoring.Rec, 0.5);
  assert.equal(cfg.scoring['Rush Yds'], 0.1);
  assert.equal(cfg.scoring['Pass TD'], 4);
});

test('leagueConfig omits bench and IR from roster slots', () => {
  const league = normalize(JSON.parse(readFileSync(
    new URL('./fixtures/league-settings.json', import.meta.url), 'utf8'))).league;
  const cfg = leagueConfig(league);
  assert.equal(cfg.rosterSlots.BN, undefined);
  assert.equal(cfg.rosterSlots.IR, undefined);
});

test('leagueConfig keeps the individual-player stat when two categories share a display name', () => {
  // Yahoo's own settings define BOTH stat_id 6 ("Interceptions", passing,
  // an individual QB's thrown picks, modifier -1) and stat_id 33
  // ("Interception", def_turnovers, a defense's takeaways, modifier +2)
  // under the identical display_name "Int". A join keyed purely by name
  // silently drops one -- and dropping the QB penalty in favour of the
  // defensive bonus scores every QB backwards.
  const league = normalize(JSON.parse(readFileSync(
    new URL('./fixtures/league-settings.json', import.meta.url), 'utf8'))).league;
  const cfg = leagueConfig(league);
  assert.equal(cfg.scoring.Int, -1);
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
