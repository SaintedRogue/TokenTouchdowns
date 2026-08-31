import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { normalize } from '../src/normalize.js';

const fixture = (n) =>
  JSON.parse(readFileSync(new URL(`./fixtures/${n}.json`, import.meta.url), 'utf8'));

test('normalize unwraps fantasy_content and merges league attrs with sub-resources', () => {
  const out = normalize(fixture('league-teams'));
  assert.equal(out.league.name, 'Definitely Not Bots');
  assert.equal(out.league.league_key, '470.l.1433971');
  assert.equal(out.league.num_teams, 4);
});

test('normalize turns the teams collection into a real array of usable objects', () => {
  const { league } = normalize(fixture('league-teams'));
  assert.ok(Array.isArray(league.teams), 'teams should be an array');
  assert.equal(league.teams.length, 4);
  // The attribute-array flattening must have happened for each team.
  assert.deepEqual(
    league.teams.map((t) => t.name).sort(),
    ['Any Given Model', 'Man Coverage Carol', 'The Turing Test Dummies', 'Token Maxxing Touchdowns'].sort(),
  );
  assert.equal(league.teams[0].team_key, '470.l.1433971.t.1');
});

test('normalize identifies the logged-in user team via is_owned_by_current_login', () => {
  const { league } = normalize(fixture('league-teams'));
  const mine = league.teams.filter((t) => t.is_owned_by_current_login === 1);
  assert.equal(mine.length, 1);
  assert.equal(mine[0].team_key, '470.l.1433971.t.4');
});

test('normalize handles the deeply nested users -> games -> leagues chain', () => {
  const out = normalize(fixture('user-leagues'));
  const leagues = out.users[0].games[0].leagues;
  assert.ok(Array.isArray(leagues));
  assert.equal(leagues[0].league_key, '470.l.1433971');
  assert.equal(out.users[0].games[0].season, '2026');
});

test('normalize exposes a team roster as an array of players', () => {
  const out = normalize(fixture('team-roster'));
  assert.equal(out.team.team_key, '470.l.1433971.t.4');
  assert.ok(Array.isArray(out.team.roster.players),
    'roster.players should be an array (empty pre-draft)');
});

test('normalize converts the paged player pool into player objects', () => {
  const { league } = normalize(fixture('league-players'));
  assert.equal(league.players.length, 3);
  for (const p of league.players) {
    assert.ok(p.player_key, 'each player has a player_key');
    assert.equal(typeof p.name.full, 'string');
  }
});

test('normalize preserves standings team_standings data', () => {
  const { league } = normalize(fixture('league-standings'));
  assert.equal(league.standings.teams.length, 4);
  assert.ok(league.standings.teams[0].team_standings, 'standings block present');
});
