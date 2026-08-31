import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createClient } from '../src/client.js';
import { loadCookieHeader } from '../src/session.js';

// Opt-in: needs a live Yahoo session in the browser profile and network access.
//   TT_LIVE=1 npm test
const live = process.env.TT_LIVE === '1';

test('live: reads the user\'s NFL leagues from the real API', { skip: !live }, async () => {
  const client = createClient({ cookieHeader: await loadCookieHeader() });
  const out = await client.get('users;use_login=1/games;game_keys=nfl/leagues');
  const league = out.users[0].games[0].leagues[0];
  assert.match(league.league_key, /^\d+\.l\.\d+$/);
  assert.ok(league.name.length > 0);
  console.log(`      league: ${league.name} (${league.league_key})`);
});

test('live: reads league teams and finds the logged-in user\'s team', { skip: !live }, async () => {
  const client = createClient({ cookieHeader: await loadCookieHeader() });
  const { league } = await client.get('league/470.l.1433971/teams');
  assert.equal(league.teams.length, league.num_teams);
  const mine = league.teams.find((t) => t.is_owned_by_current_login === 1);
  assert.ok(mine, 'exactly one team should be owned by the logged-in user');
  console.log(`      my team: ${mine.name} (${mine.team_key})`);
});

test('live: reads a team roster', { skip: !live }, async () => {
  const client = createClient({ cookieHeader: await loadCookieHeader() });
  const { team } = await client.get('team/470.l.1433971.t.4/roster');
  assert.ok(Array.isArray(team.roster.players));
  console.log(`      roster: ${team.roster.players.length} players (week ${team.roster.week})`);
});
