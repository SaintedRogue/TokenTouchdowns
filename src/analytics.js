/**
 * Bridge to the Python analytics engine (docs/draft-engine-design.md
 * section 4). `src/cli.js`'s draft/mock/lineup/playoff commands are all
 * pure Node on one side (Yahoo fetch, table rendering, argument parsing)
 * and pure Python on the other (`analytics/src/tt/cli.py`) -- this module
 * is the only thing that crosses that boundary: spawn
 * `analytics/.venv/bin/python -m tt.cli <subcommand>`, write a JSON payload
 * to its stdin, and parse exactly one JSON document back off its stdout.
 *
 * `spawn` is injectable, the same way `createClient` (src/client.js)
 * injects `fetch`: Node's own test suite must never shell out to a real
 * Python process, and `test/analytics.test.js` exercises every failure
 * mode below (a missing venv, a non-zero exit, non-JSON stdout) against a
 * scripted fake child process instead.
 *
 * FAILURE MODES THIS MODULE EXISTS TO TURN INTO A CLEAR MESSAGE, NOT A
 * STACK TRACE (see docs/draft-engine-design.md phase-2 CLI report):
 *   - The venv or `tt.cli` module is missing -- `spawn` itself errors
 *     (ENOENT), surfaced with a pointer at analytics/.venv.
 *   - Python exits non-zero -- `tt.cli`'s own `CliError` messages (a
 *     missing league.json, a missing parquet directory, a bad flag) land
 *     on stderr; that text becomes this module's AnalyticsError message
 *     verbatim, so the user sees Python's own remedy, not a generic
 *     "Python failed."
 *   - Python's stdout is not valid JSON -- `tt.cli` is documented to never
 *     let this happen (see that module's own "STDOUT DISCIPLINE" docstring
 *     section), but this module treats it as a real possibility rather
 *     than trusting that contract blindly, and reports both stdout and
 *     stderr so the corruption is diagnosable.
 */
import { spawn as nodeSpawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/** A Python-side failure (missing venv, non-zero exit, bad JSON) -- distinct
 * from a Yahoo-side or usage error so `src/cli.js` can report it plainly
 * without wrapping it in an unrelated error type. */
export class AnalyticsError extends Error {
  constructor(message) { super(message); this.name = 'AnalyticsError'; }
}

const REPO_ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');
export const DEFAULT_ANALYTICS_DIR = path.join(REPO_ROOT, 'analytics');
export const DEFAULT_PYTHON_BIN = path.join(DEFAULT_ANALYTICS_DIR, '.venv', 'bin', 'python');

/** camelCase -> kebab-case, matching argparse's `--data-dir`-style flags. */
function toKebab(key) {
  return key.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`);
}

/** Flags object -> argv tail. `true` renders as a bare flag (argparse
 * `store_true`); `false`/`undefined`/`null` are omitted entirely rather
 * than rendered as the string "false" (which argparse would just treat as
 * a truthy string value for a non-boolean flag). Shared by `buildArgs`
 * (a `tt.cli` subcommand) and `runScript` (a standalone script path) --
 * both need the identical camelCase -> `--kebab-case` conversion, just
 * with a different fixed prefix in front of it. */
function flagArgs(flags = {}) {
  const args = [];
  for (const [key, value] of Object.entries(flags)) {
    if (value === undefined || value === null || value === false) continue;
    const flagName = toKebab(key);
    args.push(value === true ? `--${flagName}` : `--${flagName}=${value}`);
  }
  return args;
}

export function buildArgs(subcommand, flags = {}) {
  return ['-m', 'tt.cli', subcommand, ...flagArgs(flags)];
}

/**
 * `spawn`/`pythonBin`/`cwd` are all injectable -- production code never
 * passes any of them and gets the real venv; tests always pass a fake
 * `spawn` (see test/analytics.test.js) and never touch a real process.
 */
export function createAnalyticsClient({
  spawn: spawnImpl = nodeSpawn,
  pythonBin = DEFAULT_PYTHON_BIN,
  cwd = DEFAULT_ANALYTICS_DIR,
} = {}) {
  /**
   * Spawn `pythonBin args...`, write `stdin` as JSON to its stdin, and
   * resolve with the parsed JSON on stdout on a clean exit -- the shared
   * mechanics behind both `run` (a `tt.cli` subcommand) and `runScript` (an
   * arbitrary standalone script path, e.g. src/draft-room-recompute.py).
   * Both callers get IDENTICAL error handling (a missing venv, a non-zero
   * exit, non-JSON stdout) for free; only how `args` is built differs.
   *
   * `stderr` (optional): a writable sink (`{ write(str) }`, e.g. `src/
   * cli.js`'s own injected `err`) that receives every stderr chunk LIVE,
   * as the child emits it -- on top of, not instead of, the accumulated
   * `stderr` string this function already builds for a failure message.
   * Exists for `tt trade --find`: `tt.cli`'s `cmd_trade` writes an
   * up-front candidate-count estimate to ITS stderr before its one
   * (possibly slow) blocking call, specifically so the command does not
   * appear to hang -- and that text is invisible to a caller who never
   * passes this, since a SUCCESSFUL run previously had no path from the
   * accumulated buffer to anywhere a user could see it.
   */
  async function execPython(args, { stdin = {}, stderr: stderrSink = undefined } = {}, label = args.join(' ')) {
    let child;
    try {
      child = spawnImpl(pythonBin, args, { cwd });
    } catch (e) {
      // A synchronous throw is unusual for child_process.spawn (it
      // normally reports failure via an async 'error' event, handled
      // below) but is exactly the shape a hand-written fake `spawn` might
      // take, so both paths are handled the same way.
      throw new AnalyticsError(
        `Could not start the analytics engine (${pythonBin}): ${e.message}. ` +
        'Is analytics/.venv set up? See analytics/README.md.',
      );
    }

    // A stdin write failing (e.g. the process never actually started) must
    // not crash this process with an unhandled 'error' event -- the
    // authoritative failure signal is the child's own 'error'/'close'
    // below, which the write failure will also trigger.
    child.stdin?.on?.('error', () => {});

    // Accumulated via 'data', not read via a separate 'end'-driven promise:
    // Node only emits a child's 'close' event once its stdio streams have
    // themselves ended, so every 'data' event is guaranteed to have already
    // fired by the time 'close' resolves the promise below. A fake child in
    // a test is free to never emit 'end' at all (see test/analytics.test.js)
    // as long as it emits 'close' after its 'data' events, same contract.
    let stdout = '';
    let stderr = '';
    child.stdout?.on('data', (chunk) => { stdout += chunk.toString(); });
    child.stderr?.on('data', (chunk) => {
      const text = chunk.toString();
      stderr += text;
      stderrSink?.write?.(text);
    });

    // Writing stdin happens SYNCHRONOUSLY inside this executor, before the
    // 'close'/'error' listeners below ever get a chance to fire (those are
    // always asynchronous, whether from a real child process or a
    // scripted fake) -- writing after the `await` would race a fast fake
    // (or a fast-failing real process) that closes before Node ever got
    // back around to writing its stdin.
    const exitCode = await new Promise((resolve, reject) => {
      child.on('error', (err) => reject(new AnalyticsError(
        `Could not start the analytics engine (${pythonBin}): ${err.message}. ` +
        'Is analytics/.venv set up? See analytics/README.md.',
      )));
      child.on('close', (code) => resolve(code));
      child.stdin.write(JSON.stringify(stdin ?? {}));
      child.stdin.end();
    });

    if (exitCode !== 0) {
      throw new AnalyticsError(
        `Analytics engine (${label}) exited with code ${exitCode}` +
        (stderr.trim() ? `:\n${stderr.trim()}` : ''),
      );
    }

    try {
      return JSON.parse(stdout);
    } catch {
      throw new AnalyticsError(
        `Analytics engine (${label}) did not return valid JSON.` +
        (stderr.trim() ? ` stderr:\n${stderr.trim()}` : '') +
        (stdout.trim() ? ` stdout:\n${stdout.trim().slice(0, 500)}` : ' (empty stdout)'),
      );
    }
  }

  /**
   * Run one `tt.cli` subcommand. `flags` becomes CLI arguments (see
   * `buildArgs`); `stdin` is JSON-encoded and written to the child's
   * stdin, then the stream is closed (`tt.cli` blocks reading stdin until
   * EOF -- see that module's own docstring). Resolves with the parsed
   * JSON result on a clean exit, rejects with `AnalyticsError` otherwise.
   */
  async function run(subcommand, { flags = {}, stdin = {}, stderr } = {}) {
    return execPython(buildArgs(subcommand, flags), { stdin, stderr }, `tt.cli ${subcommand}`);
  }

  /**
   * Run a standalone Python script by absolute path -- NOT a `tt.cli`
   * subcommand -- with the SAME stdin-JSON-in / stdout-JSON-out contract
   * and the SAME error handling as `run`. Exists for
   * src/draft-room-recompute.py: a script that lives outside analytics/
   * (this feature's branch keeps that directory untouched) but still
   * imports the installed `tt` package directly, so it can recompute
   * survival/recommendations against an ALREADY-BUILT board without
   * re-running `tt.cli board`/`pick`'s own expensive Monte Carlo step --
   * see that script's module docstring and docs/draft-room-design.md
   * section 3 for why that split exists at all.
   *
   * `flags` (rare -- draft-room-recompute.py takes everything via stdin)
   * goes through the identical kebab-case `buildArgs` conversion as `run`,
   * just without the `-m tt.cli <subcommand>` prefix.
   */
  async function runScript(scriptPath, { flags = {}, stdin = {}, stderr } = {}) {
    return execPython([scriptPath, ...flagArgs(flags)], { stdin, stderr }, scriptPath);
  }

  return { run, runScript, pythonBin, cwd };
}
