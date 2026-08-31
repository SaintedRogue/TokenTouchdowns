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
