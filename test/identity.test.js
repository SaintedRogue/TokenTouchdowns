import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  yahooPlayerId,
  buildCrosswalk,
  lookupByYahooKey,
  normalizeName,
  buildAdpIndex,
  matchAdp,
  adpMatchState,
} from '../src/identity.js';

const RECORDS = [
  { yahooId: '26686', sleeperId: '1466', gsisId: '00-0030506', name: 'Travis Kelce', position: 'TE' },
  { yahooId: '30977', sleeperId: '4034', gsisId: '00-0033280', name: 'Christian McCaffrey', position: 'RB' },
];

test('yahooPlayerId extracts the numeric id from a player_key', () => {
  assert.equal(yahooPlayerId('470.p.26686'), '26686');
});

test('yahooPlayerId returns null for a key that is not a player key', () => {
  assert.equal(yahooPlayerId('470.l.1433971.t.4'), null);
  assert.equal(yahooPlayerId(undefined), null);
});

test('buildCrosswalk indexes records by yahoo id', () => {
  const cw = buildCrosswalk(RECORDS);
  assert.equal(cw.get('26686').sleeperId, '1466');
  assert.equal(cw.size, 2);
});

test('lookupByYahooKey resolves a Yahoo player_key to the crosswalk record', () => {
  const cw = buildCrosswalk(RECORDS);
  assert.equal(lookupByYahooKey(cw, '470.p.26686').gsisId, '00-0030506');
});

test('lookupByYahooKey returns null for an unknown player', () => {
  assert.equal(lookupByYahooKey(buildCrosswalk(RECORDS), '470.p.99999'), null);
});

test('normalizeName lowercases and strips punctuation', () => {
  assert.equal(normalizeName("A.J. Brown"), 'aj brown');
});

test('normalizeName strips generational suffixes', () => {
  assert.equal(normalizeName('James Cook III'), 'james cook');
  assert.equal(normalizeName('Odell Beckham Jr.'), 'odell beckham');
  assert.equal(normalizeName('Michael Pittman Sr'), 'michael pittman');
  assert.equal(normalizeName('Marvin Harrison II'), 'marvin harrison');
  assert.equal(normalizeName('Kenneth Walker IV'), 'kenneth walker');
  assert.equal(normalizeName('Some Player V'), 'some player');
});

test('normalizeName collapses repeated whitespace', () => {
  assert.equal(normalizeName('  Travis   Kelce '), 'travis kelce');
});

test('normalizeName folds diacritics so sources using different encodings still match', () => {
  // Live FFC feed spells this "Eddy Piñeiro"; Yahoo sends "Pineiro".
  assert.equal(normalizeName('Eddy Piñeiro'), 'eddy pineiro');
});

test('matchAdp joins on normalised name and position', () => {
  const idx = buildAdpIndex([{ name: 'Travis Kelce', position: 'TE', adp: 40.2 }]);
  assert.equal(matchAdp(idx, { name: 'Travis Kelce', position: 'TE' }).adp, 40.2);
});

test('matchAdp matches across a suffix difference between sources', () => {
  // FFC says "James Cook", Yahoo says "James Cook III".
  const idx = buildAdpIndex([{ name: 'James Cook', position: 'RB', adp: 22.5 }]);
  assert.equal(matchAdp(idx, { name: 'James Cook III', position: 'RB' }).adp, 22.5);
});

test('matchAdp refuses to match across positions', () => {
  const idx = buildAdpIndex([{ name: 'Mike Williams', position: 'WR', adp: 90 }]);
  assert.equal(matchAdp(idx, { name: 'Mike Williams', position: 'TE' }), null);
});

test('matchAdp returns null when a name+position is ambiguous', () => {
  // Two distinct players share a normalised name AND position: never guess.
  const idx = buildAdpIndex([
    { name: 'Josh Allen', position: 'QB', adp: 30, team: 'BUF' },
    { name: 'Josh Allen', position: 'QB', adp: 200, team: 'JAX' },
  ]);
  assert.equal(matchAdp(idx, { name: 'Josh Allen', position: 'QB' }), null);
});

test('matchAdp resolves an otherwise-ambiguous name using team as a tiebreaker', () => {
  const idx = buildAdpIndex([
    { name: 'Josh Allen', position: 'QB', adp: 30, team: 'BUF' },
    { name: 'Josh Allen', position: 'QB', adp: 200, team: 'JAX' },
  ]);
  assert.equal(matchAdp(idx, { name: 'Josh Allen', position: 'QB', team: 'BUF' }).adp, 30);
});

test('matchAdp treats duplicate rows for the same name+position+team as ambiguous, not a false resolution', () => {
  // Two source rows collide on every field including team. This must be just
  // as ambiguous as differing-team duplicates, not silently "resolved" to
  // whichever row the team-qualified key happened to keep last.
  const idx = buildAdpIndex([
    { name: 'Josh Allen', position: 'QB', adp: 30, team: 'BUF' },
    { name: 'Josh Allen', position: 'QB', adp: 31, team: 'BUF' },
  ]);
  assert.equal(matchAdp(idx, { name: 'Josh Allen', position: 'QB', team: 'BUF' }), null);
});

test('matchAdp ignores a team that matches no indexed team, falling back to the unambiguous base match', () => {
  // Team is a tiebreaker only, never a requirement: sources disagree on team
  // during preseason trades.
  const idx = buildAdpIndex([{ name: 'Travis Kelce', position: 'TE', adp: 40.2, team: 'KC' }]);
  assert.equal(matchAdp(idx, { name: 'Travis Kelce', position: 'TE', team: 'DEN' }).adp, 40.2);
});

test('matchAdp is case-insensitive on position', () => {
  const idx = buildAdpIndex([{ name: 'Christian McCaffrey', position: 'RB', adp: 3.1 }]);
  assert.equal(matchAdp(idx, { name: 'Christian McCaffrey', position: 'rb' }).adp, 3.1);
});

test('matchAdp is case-insensitive on team', () => {
  const idx = buildAdpIndex([
    { name: 'Josh Allen', position: 'QB', adp: 30, team: 'BUF' },
    { name: 'Josh Allen', position: 'QB', adp: 200, team: 'JAX' },
  ]);
  assert.equal(matchAdp(idx, { name: 'Josh Allen', position: 'QB', team: 'buf' }).adp, 30);
});

test('matchAdp reconciles FFC "PK" with Yahoo "K"', () => {
  const idx = buildAdpIndex([{ name: 'Eddy Pineiro', position: 'PK', adp: 180 }]);
  assert.equal(matchAdp(idx, { name: 'Eddy Pineiro', position: 'K' }).adp, 180);
});

test('buildAdpIndex keys team defenses by team abbreviation, ignoring the defense name entirely', () => {
  const idx = buildAdpIndex([
    { name: 'Seattle Defense', position: 'DEF', adp: 85, team: 'SEA' },
    { name: 'NY Giants Defense', position: 'DEF', adp: 140, team: 'NYG' },
  ]);
  // Names in the query are irrelevant for DEF -- only team abbreviation
  // decides the match (spec rule 5).
  assert.equal(matchAdp(idx, { name: 'New York', position: 'DEF', team: 'NYG' }).adp, 140);
  assert.equal(matchAdp(idx, { name: 'anything at all', position: 'DEF', team: 'SEA' }).adp, 85);
});

test('matchAdp never falls back to name matching for a team defense', () => {
  const idx = buildAdpIndex([{ name: 'Seattle Defense', position: 'DEF', adp: 85, team: 'SEA' }]);
  // Exact name match, wrong team: must not resolve by name.
  assert.equal(matchAdp(idx, { name: 'Seattle Defense', position: 'DEF', team: 'NYG' }), null);
  // No team supplied at all: DEF can never be resolved by name alone.
  assert.equal(matchAdp(idx, { name: 'Seattle Defense', position: 'DEF' }), null);
});

test('matchAdp reconciles the DST alias with DEF for team defenses', () => {
  const idx = buildAdpIndex([{ name: 'Seattle Defense', position: 'DST', adp: 85, team: 'SEA' }]);
  assert.equal(matchAdp(idx, { name: 'Seattle', position: 'DEF', team: 'SEA' }).adp, 85);
});

test('buildAdpIndex never indexes a record whose normalised name is empty, and matchAdp never matches on one', () => {
  // A malformed row (null name) and a malformed query ("III", which strips
  // to nothing) must not collide on an empty-string join key.
  const idx = buildAdpIndex([{ name: null, position: 'RB', adp: 7.7 }]);
  assert.equal(idx.size, 0);
  assert.equal(matchAdp(idx, { name: 'III', position: 'RB' }), null);
  assert.equal(matchAdp(idx, { name: '---', position: 'RB' }), null);
});

test('matchAdp does not let a forged separator in a position string reach a team-qualified key', () => {
  const idx = buildAdpIndex([
    { name: 'Josh Allen', position: 'QB', adp: 30, team: 'BUF' },
    { name: 'Josh Allen', position: 'QB', adp: 200, team: 'JAX' },
  ]);
  assert.equal(matchAdp(idx, { name: 'Josh Allen', position: 'QB|BUF' }), null);
});

test('adpMatchState distinguishes matched, ambiguous, and absent while matchAdp keeps returning record|null', () => {
  const idx = buildAdpIndex([
    { name: 'Travis Kelce', position: 'TE', adp: 40.2 },
    { name: 'Josh Allen', position: 'QB', adp: 30, team: 'BUF' },
    { name: 'Josh Allen', position: 'QB', adp: 200, team: 'JAX' },
  ]);
  assert.equal(adpMatchState(idx, { name: 'Travis Kelce', position: 'TE' }), 'matched');
  assert.equal(adpMatchState(idx, { name: 'Josh Allen', position: 'QB' }), 'ambiguous');
  assert.equal(adpMatchState(idx, { name: 'Nobody', position: 'WR' }), 'absent');
  assert.equal(matchAdp(idx, { name: 'Josh Allen', position: 'QB' }), null);
  assert.equal(matchAdp(idx, { name: 'Nobody', position: 'WR' }), null);
});

test('matchAdp returns null for an unknown player rather than throwing', () => {
  const idx = buildAdpIndex([{ name: 'Travis Kelce', position: 'TE', adp: 40.2 }]);
  assert.equal(matchAdp(idx, { name: 'Nobody', position: 'WR' }), null);
});
