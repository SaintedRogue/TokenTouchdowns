import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { createAnalyticsClient, AnalyticsError } from '../src/analytics.js';

/**
 * A fake child_process.ChildProcess: an EventEmitter carrying `.stdin`
 * (records writes), `.stdout`/`.stderr` (their own EventEmitters), so tests
 * never spawn a real Python process -- the whole point of the injectable
 * `spawn` (mirrors `createClient`'s injectable `fetch` in src/client.js).
 */
function fakeChild() {
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.stdinWrites = [];
  child.stdinEnded = false;
  child.stdin = {
    write(chunk) { child.stdinWrites.push(chunk); },
    end() { child.stdinEnded = true; },
  };
  return child;
}

/** Build a spawn stub that captures its call and returns a scripted child. */
function scriptedSpawn({ stdout = '', stderr = '', code = 0, spawnError = null } = {}) {
  const calls = [];
  const spawn = (command, args, options) => {
    const child = fakeChild();
    calls.push({ command, args, options, child });
    // Emit asynchronously, like a real child process would -- a synchronous
    // emit would fire before this function's caller has attached listeners.
    queueMicrotask(() => {
      if (spawnError) {
        child.emit('error', spawnError);
        return;
      }
      if (stdout) child.stdout.emit('data', Buffer.from(stdout));
      if (stderr) child.stderr.emit('data', Buffer.from(stderr));
      child.emit('close', code);
    });
    return child;
  };
  spawn.calls = calls;
  return spawn;
}

test('run spawns python -m tt.cli <subcommand> in the analytics dir', async () => {
  const spawn = scriptedSpawn({ stdout: '{"ok":true}\n' });
  const client = createAnalyticsClient({ spawn, pythonBin: '/fake/python', cwd: '/fake/analytics' });
  const result = await client.run('board', { flags: { teams: 10 } });
  assert.deepEqual(result, { ok: true });
  assert.equal(spawn.calls.length, 1);
  const { command, args, options } = spawn.calls[0];
  assert.equal(command, '/fake/python');
  assert.deepEqual(args, ['-m', 'tt.cli', 'board', '--teams=10']);
  assert.equal(options.cwd, '/fake/analytics');
});

test('run feeds the stdin payload as JSON and closes stdin', async () => {
  const spawn = scriptedSpawn({ stdout: '{}\n' });
  const client = createAnalyticsClient({ spawn, pythonBin: '/fake/python', cwd: '/x' });
  await client.run('lineup', { stdin: { roster: [{ player_id: 'a' }] } });
  const { child } = spawn.calls[0];
  assert.deepEqual(child.stdinWrites.map((w) => JSON.parse(w)), [{ roster: [{ player_id: 'a' }] }]);
  assert.equal(child.stdinEnded, true);
});

test('run defaults stdin to {} when none is given', async () => {
  const spawn = scriptedSpawn({ stdout: '{}\n' });
  const client = createAnalyticsClient({ spawn, pythonBin: '/fake/python', cwd: '/x' });
  await client.run('board');
  const { child } = spawn.calls[0];
  assert.deepEqual(child.stdinWrites.map((w) => JSON.parse(w)), [{}]);
});

test('run converts camelCase flag keys to kebab-case CLI flags', async () => {
  const spawn = scriptedSpawn({ stdout: '{}\n' });
  const client = createAnalyticsClient({ spawn, pythonBin: '/fake/python', cwd: '/x' });
  await client.run('board', { flags: { dataDir: '/data', mcN: 30, adpNoise: 6.0 } });
  assert.deepEqual(spawn.calls[0].args, [
    '-m', 'tt.cli', 'board', '--data-dir=/data', '--mc-n=30', '--adp-noise=6',
  ]);
});

test('run renders a bare boolean-true flag with no value, and omits false/undefined/null', async () => {
  const spawn = scriptedSpawn({ stdout: '{}\n' });
  const client = createAnalyticsClient({ spawn, pythonBin: '/fake/python', cwd: '/x' });
  await client.run('board', {
    flags: { conditional: true, verbose: false, season: undefined, adp: null, teams: 4 },
  });
  assert.deepEqual(spawn.calls[0].args, ['-m', 'tt.cli', 'board', '--conditional', '--teams=4']);
});

test('run parses stdout JSON on a clean exit', async () => {
  const spawn = scriptedSpawn({ stdout: '{"players":[{"a":1}]}\n' });
  const client = createAnalyticsClient({ spawn, pythonBin: '/fake/python', cwd: '/x' });
  const result = await client.run('board');
  assert.deepEqual(result, { players: [{ a: 1 }] });
});

test('run throws AnalyticsError on a non-zero exit, carrying stderr', async () => {
  const spawn = scriptedSpawn({
    code: 1,
    // Valid JSON on stdout too -- so this only fails the exit-code check,
    // not the "stdout isn't JSON" check. Distinguishes the two failure
    // paths: a mutant that stops checking `exitCode` but still falls
    // through to the JSON-parse branch would pass here (stdout parses
    // fine) unless this test also asserts on the specific "exited with
    // code" wording, which it does below.
    stdout: '{}\n',
    stderr: 'League config not found at data/league.json. Run: tt league export\n',
  });
  const client = createAnalyticsClient({ spawn, pythonBin: '/fake/python', cwd: '/x' });
  await assert.rejects(() => client.run('board'), (e) => {
    assert.ok(e instanceof AnalyticsError);
    assert.match(e.message, /exited with code 1/);
    assert.match(e.message, /League config not found/);
    return true;
  });
});

test('run throws AnalyticsError when stdout is not JSON (e.g. a stray warning leaked through)', async () => {
  const spawn = scriptedSpawn({ code: 0, stdout: 'UserWarning: something\n{"ok":true}\n' });
  const client = createAnalyticsClient({ spawn, pythonBin: '/fake/python', cwd: '/x' });
  await assert.rejects(() => client.run('board'), (e) => {
    assert.ok(e instanceof AnalyticsError);
    assert.match(e.message, /valid JSON/i);
    return true;
  });
});

test('run throws a helpful AnalyticsError when the python binary cannot be spawned at all', async () => {
  const err = Object.assign(new Error('spawn /fake/python ENOENT'), { code: 'ENOENT' });
  const spawn = scriptedSpawn({ spawnError: err });
  const client = createAnalyticsClient({ spawn, pythonBin: '/fake/python', cwd: '/x' });
  await assert.rejects(() => client.run('board'), (e) => {
    assert.ok(e instanceof AnalyticsError);
    assert.match(e.message, /venv/i);
    return true;
  });
});

test('run with no flags/stdin still calls the subcommand cleanly', async () => {
  const spawn = scriptedSpawn({ stdout: '{}\n' });
  const client = createAnalyticsClient({ spawn, pythonBin: '/fake/python', cwd: '/x' });
  const result = await client.run('mock');
  assert.deepEqual(result, {});
  assert.deepEqual(spawn.calls[0].args, ['-m', 'tt.cli', 'mock']);
});

test('run relays stderr chunks live to an optional stderr sink, on top of buffering them for a failure message', async () => {
  // `tt trade --find` prints an up-front candidate-count estimate to
  // Python's own stderr before its one (possibly slow) blocking call --
  // see analytics/src/tt/cli.py's cmd_trade docstring. Without this relay
  // that text is invisible: it only ever reached the accumulated `stderr`
  // string this module already builds for a FAILURE message, and a
  // successful run threw that buffer away unread. An optional `stderr`
  // sink lets a caller see it live, on a successful run too.
  const spawn = scriptedSpawn({ stdout: '{"ok":true}\n', stderr: 'up to 214 candidates...\n', code: 0 });
  const client = createAnalyticsClient({ spawn, pythonBin: '/fake/python', cwd: '/x' });
  const chunks = [];
  const sink = { write: (s) => chunks.push(s) };
  const result = await client.run('trade', { stderr: sink });
  assert.deepEqual(result, { ok: true });
  assert.equal(chunks.join(''), 'up to 214 candidates...\n');
});

test('run works with no stderr sink given -- existing callers are unaffected', async () => {
  const spawn = scriptedSpawn({ stdout: '{"ok":true}\n', stderr: 'some progress\n', code: 0 });
  const client = createAnalyticsClient({ spawn, pythonBin: '/fake/python', cwd: '/x' });
  const result = await client.run('trade');
  assert.deepEqual(result, { ok: true });
});

test('default pythonBin and cwd point at analytics/.venv relative to the repo', () => {
  const client = createAnalyticsClient({ spawn: scriptedSpawn() });
  assert.match(client.pythonBin, /analytics[/\\]\.venv[/\\]bin[/\\]python$/);
  assert.match(client.cwd, /analytics$/);
});
