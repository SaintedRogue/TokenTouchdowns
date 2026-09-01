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

// --- roster_positions: a collection with no `count` sibling -----------------
//
// Yahoo's league settings return roster_positions as a bare array of
// single-key-wrapped items -- `[{roster_position: {...}}, {roster_position: {...}}, ...]`
// with no `count` anywhere. The old code assumed every array was an
// attribute-fragment list to be merged into one object, so nine positions
// collapsed into whichever one was assigned last (IR). These tests use the
// real committed fixture, which reproduces the bug exactly.

test('normalize turns roster_positions (a collection with no count) into an array of every position, not just the last one', () => {
  const { league } = normalize(fixture('league-settings'));
  const positions = league.settings.roster_positions;
  assert.ok(Array.isArray(positions), 'roster_positions should be an array');
  assert.equal(positions.length, 9, 'all 9 roster positions must survive, not just the last');
});

test('normalize preserves every roster position with its correct count, unwrapped from its wrapper', () => {
  const { league } = normalize(fixture('league-settings'));
  const positions = league.settings.roster_positions;
  // Collection items must be unwrapped from {roster_position: X} down to X.
  assert.ok(!('roster_position' in positions[0]), 'items should be unwrapped, not left as {roster_position: X}');
  const byPosition = Object.fromEntries(positions.map((p) => [p.position, p.count]));
  assert.deepEqual(byPosition, {
    QB: 1, RB: 2, WR: 2, TE: 1, 'W/R/T': 1, K: 1, DEF: 1, BN: 6, IR: 2,
  });
});

test('normalize still merges the mixed roster shape (numeric key + real attributes) instead of turning it into an array', () => {
  // roster: { "0": { players: [] }, coverage_type: "week", week: 1, ... } has a
  // numeric key ALONGSIDE plain attributes -- the merge-up behaviour that
  // splices numeric-keyed fragments into the parent must still apply here.
  const out = normalize(fixture('team-roster'));
  assert.equal(Array.isArray(out.team.roster), false, 'the mixed roster shape must not become an array');
  assert.deepEqual(out.team.roster, {
    players: [], coverage_type: 'week', week: 1, is_prescoring: 0, is_editable: 0,
  });
});

test('normalize still turns a count-bearing numeric-keyed collection into an array (regression guard)', () => {
  const raw = {
    fantasy_content: {
      league: {
        teams: { 0: { team: { name: 'A' } }, 1: { team: { name: 'B' } }, count: 2 },
      },
    },
  };
  const out = normalize(raw);
  assert.ok(Array.isArray(out.league.teams), 'a count-bearing collection should still become an array');
  assert.equal(out.league.teams.length, 2);
  assert.deepEqual(out.league.teams.map((t) => t.name).sort(), ['A', 'B']);
});
