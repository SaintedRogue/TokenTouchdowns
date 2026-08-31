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
