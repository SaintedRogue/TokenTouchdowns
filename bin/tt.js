#!/usr/bin/env node
import { parseArgs, runCommand } from '../src/cli.js';
import { createClient } from '../src/client.js';
import { loadCookieHeader, login, isInteractive } from '../src/session.js';

const parsed = parseArgs(process.argv.slice(2));
const interactive = isInteractive();

// Commands that need no API client.
if (parsed.command === 'help') process.exit(await runCommand(parsed, {}));
if (parsed.command === 'login') process.exit((await login()) ? 0 : 1);
if (parsed.command === 'sources') process.exit(await runCommand(parsed, {}));
if (parsed.command === 'sync') process.exit(await runCommand(parsed, {}));

const makeClient = async () => createClient({ cookieHeader: await loadCookieHeader() });

let code = await runCommand(parsed, { client: await makeClient(), interactive });

// Design doc §9.1: re-authenticate in place only where a human can complete it.
// Exit code 2 means the session was rejected.
if (code === 2 && interactive) {
  if (await login()) {
    code = await runCommand(parsed, { client: await makeClient(), interactive: false });
  }
}

process.exit(code);
