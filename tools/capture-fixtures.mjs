// DEV TOOL (not shipped): pulls real v2 responses and scrubs PII before they
// become public test fixtures. The repo is public -- raw captures contain the
// account GUID, other league members' real names, and LIVE league invitation
// tokens (anyone holding one can join a private league).
//
// The PII values themselves live in tools/scrub-map.local.json, which is
// gitignored. Copy scrub-map.example.json and fill it in.
//
//   TT_JAR=/path/to/cookies.txt node tools/capture-fixtures.mjs
import { writeFileSync, readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';

const JAR = process.env.TT_JAR;
if (!JAR) { console.error('set TT_JAR=/path/to/cookies.txt'); process.exit(1); }

const MAP = JSON.parse(readFileSync(new URL('./scrub-map.local.json', import.meta.url), 'utf8'));
const UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36';
const BASE = 'https://pub-api-ro.fantasysports.yahoo.com/fantasy/v2';

const names = Object.keys(MAP.names).join('|');
const SCRUB = [
  [new RegExp(MAP.guid, 'g'), 'GUIDTESTFIXTURE0000000001'],
  [/[A-Z0-9]{26}/g, 'GUIDTESTFIXTURE0000000002'],
  [new RegExp(`\\b(${names})\\b`, 'g'), (m) => MAP.names[m]],
  [new RegExp(`\\b${MAP.usernamePrefix}-[A-Z0-9]+\\b`, 'g'), 'testuser-0001'],
  [/[\w.+-]+@[\w-]+\.[\w.]+/g, 'redacted@example.test'],
  [/([?&](?:i?key)=)[A-Za-z0-9]+/g, '$1REDACTEDINVITEKEY'],
  [/"(short_invitation_url|invitation_url)":\s*"[^"]*"/g, '"$1": ""'],
  // Server-side response timing: pure churn that re-diffs every fixture on
  // each capture. The normaliser discards it as metadata anyway.
  [/"time":\s*"[\d.]+ms"/g, '"time": "0ms"'],
];
const scrub = (s) => SCRUB.reduce((acc, [re, rep]) => acc.replace(re, rep), s);

const get = (p) => execFileSync('curl', ['-s', '--cookie', JAR, '-A', UA, `${BASE}/${p}`],
  { encoding: 'utf8', maxBuffer: 64e6 });

const TARGETS = {
  'league-teams':     'league/470.l.1433971/teams?format=json',
  'league-standings': 'league/470.l.1433971/standings?format=json',
  'team-roster':      'team/470.l.1433971.t.4/roster?format=json',
  'user-leagues':     'users;use_login=1/games;game_keys=nfl/leagues?format=json',
  'league-players':   'league/470.l.1433971/players;start=0;count=3?format=json',
  'league-settings':  'league/470.l.1433971/settings?format=json',
  'team-matchups':    'team/470.l.1433971.t.4/matchups?format=json',
  'league-players-qb': 'league/470.l.1433971/players;status=A;position=QB;sort=OR;start=0;count=5?format=json',
};

for (const [name, p] of Object.entries(TARGETS)) {
  const clean = scrub(get(p));
  try {
    writeFileSync(`test/fixtures/${name}.json`, JSON.stringify(JSON.parse(clean), null, 2) + '\n');
    console.log(`  ${name.padEnd(18)} ok`);
  } catch { console.log(`  ${name}: NOT JSON`); }
}
