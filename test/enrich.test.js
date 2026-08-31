import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildCrosswalk, buildAdpIndex } from '../src/identity.js';
import { enrichPlayers } from '../src/enrich.js';

const CROSSWALK = buildCrosswalk([
  { yahooId: '26686', sleeperId: '1466', name: 'Travis Kelce', position: 'TE',
    team: 'KC', injuryStatus: null },
  { yahooId: '30977', sleeperId: '4034', name: 'Christian McCaffrey', position: 'RB',
    team: 'SF', injuryStatus: 'Questionable' },
]);
const ADP = buildAdpIndex([
  { name: 'Travis Kelce', position: 'TE', team: 'KC', adp: 40.2 },
]);

const YAHOO = [
  { player_key: '470.p.26686', name: { full: 'Travis Kelce' }, display_position: 'TE' },
  { player_key: '470.p.30977', name: { full: 'Christian McCaffrey' }, display_position: 'RB' },
  { player_key: '470.p.99999', name: { full: 'Unknown Guy' }, display_position: 'WR' },
];

test('enrichPlayers attaches ADP to players that match', () => {
  const { players } = enrichPlayers(YAHOO, {
    crosswalk: CROSSWALK, adpIndex: ADP, capabilities: ['adp'] });
  assert.equal(players.find((p) => p.name.full === 'Travis Kelce').adp, 40.2);
});

test('enrichPlayers attaches injury status from the crosswalk', () => {
  const { players } = enrichPlayers(YAHOO, {
    crosswalk: CROSSWALK, adpIndex: ADP, capabilities: ['injury'] });
  assert.equal(players.find((p) => p.name.full === 'Christian McCaffrey').injury,
    'Questionable');
});

test('enrichPlayers leaves unmatched players untouched rather than failing', () => {
  const { players } = enrichPlayers(YAHOO, {
    crosswalk: CROSSWALK, adpIndex: ADP, capabilities: ['adp', 'injury'] });
  const unknown = players.find((p) => p.name.full === 'Unknown Guy');
  assert.equal(unknown.adp, undefined);
  assert.equal(unknown.injury, undefined);
  assert.equal(unknown.name.full, 'Unknown Guy');
});

test('enrichPlayers only attaches the capabilities that were requested', () => {
  const { players } = enrichPlayers(YAHOO, {
    crosswalk: CROSSWALK, adpIndex: ADP, capabilities: ['injury'] });
  assert.equal(players.find((p) => p.name.full === 'Travis Kelce').adp, undefined);
});

test('enrichPlayers reports match rates so silent degradation is visible', () => {
  const { stats } = enrichPlayers(YAHOO, {
    crosswalk: CROSSWALK, adpIndex: ADP, capabilities: ['adp', 'injury'] });
  assert.equal(stats.total, 3);
  assert.equal(stats.adp.matched, 1);
  assert.equal(stats.adp.matched + stats.adp.ambiguous + stats.adp.absent, stats.total,
    'every player must be classified exactly once');
  assert.equal(stats.injury.matched, 1);
});

test('enrichPlayers returns players unchanged when no capabilities are requested', () => {
  const { players } = enrichPlayers(YAHOO, {
    crosswalk: CROSSWALK, adpIndex: ADP, capabilities: [] });
  assert.deepEqual(players, YAHOO);
});

test('enrichPlayers attaches no ADP when the match is ambiguous', () => {
  // Two ADP rows identical in name, position AND team: the team tiebreaker
  // cannot separate them, so the matcher must refuse to choose. A wrong ADP on
  // a plausible player is worse than a missing one -- it gets drafted on.
  const crosswalk = buildCrosswalk([
    { yahooId: '55', name: 'Josh Allen', position: 'QB', team: 'BUF', injuryStatus: null },
  ]);
  const adpIndex = buildAdpIndex([
    { name: 'Josh Allen', position: 'QB', team: 'BUF', adp: 30 },
    { name: 'Josh Allen', position: 'QB', team: 'BUF', adp: 200 },
  ]);
  const players = [
    { player_key: '470.p.55', name: { full: 'Josh Allen' }, display_position: 'QB' },
  ];

  const { players: out, stats } = enrichPlayers(players, {
    crosswalk, adpIndex, capabilities: ['adp'] });

  assert.equal(out[0].adp, undefined, 'must not guess between two candidates');
  assert.equal(stats.adp.ambiguous, 1);
  assert.equal(stats.adp.matched, 0);
  assert.equal(stats.adp.matched + stats.adp.ambiguous + stats.adp.absent, stats.total);
});

test('enrichPlayers passes through a player with no player_key without throwing', () => {
  const { players: out, stats } = enrichPlayers(
    [{ name: { full: 'No Key Guy' }, display_position: 'WR' }],
    { crosswalk: CROSSWALK, adpIndex: ADP, capabilities: ['adp', 'injury'] });
  assert.equal(out[0].name.full, 'No Key Guy');
  assert.equal(out[0].adp, undefined);
  assert.equal(out[0].injury, undefined);
  assert.equal(stats.total, 1);
});

test('enrichPlayers matches a team defense using the player\'s own team abbreviation', () => {
  // Sleeper gives team defenses no yahoo_id, so they never appear in the
  // crosswalk. Without falling back to Yahoo's editorial_team_abbr, every
  // defense silently resolves to "absent".
  const crosswalk = buildCrosswalk([]);           // deliberately empty: no DEF ever present
  const adpIndex = buildAdpIndex([
    { name: 'Atlanta Defense', position: 'DEF', team: 'ATL', adp: 120.5 },
  ]);
  const players = [{
    player_key: '470.p.100001',
    name: { full: 'Falcons' },
    display_position: 'DEF',
    editorial_team_abbr: 'Atl',                   // note: Yahoo sends mixed case
  }];

  const { players: out, stats } = enrichPlayers(players, {
    crosswalk, adpIndex, capabilities: ['adp'] });

  assert.equal(out[0].adp, 120.5);
  assert.equal(stats.adp.matched, 1);
  assert.equal(stats.adp.absent, 0);
});

test('enrichPlayers resolves the ADP tiebreaker with Yahoo\'s team, not the crosswalk\'s', () => {
  // Yahoo is the league of record and the source of the TEAM column the user
  // reads; Sleeper is a third party whose team can lag a trade. Two FFC rows
  // collide on name+position, so the team decides which one resolves --
  // meaning a stale crosswalk team would attach a DIFFERENT player's ADP.
  // This is the only path in the module that can produce a wrong value, so
  // the operand order is load-bearing and pinned here.
  const crosswalk = buildCrosswalk([
    { yahooId: '55', name: 'Josh Allen', position: 'QB', team: 'JAX', injuryStatus: null },
  ]);
  const adpIndex = buildAdpIndex([
    { name: 'Josh Allen', position: 'QB', team: 'BUF', adp: 30 },
    { name: 'Josh Allen', position: 'QB', team: 'JAX', adp: 200 },
  ]);
  const players = [{
    player_key: '470.p.55',
    name: { full: 'Josh Allen' },
    display_position: 'QB',
    editorial_team_abbr: 'Buf',        // Yahoo, current — note mixed case
  }];

  const { players: out, stats } = enrichPlayers(players, {
    crosswalk, adpIndex, capabilities: ['adp'] });

  assert.equal(out[0].adp, 30, 'must use Yahoo\'s BUF, not the crosswalk\'s stale JAX');
  assert.notEqual(out[0].adp, 200, 'attaching the JAX row would be another player\'s ADP');
  assert.equal(stats.adp.matched, 1);
});
