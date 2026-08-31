import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createClient, SessionExpiredError, YahooApiError } from '../src/client.js';

const okJson = (body) => async () => ({
  ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body),
});

test('get targets pub-api-ro v2 and always requests JSON', async () => {
  let seenUrl;
  const client = createClient({
    cookieHeader: 'T=x; Y=y',
    fetch: async (url) => { seenUrl = url; return okJson({ fantasy_content: {} })(); },
  });
  await client.get('league/470.l.1/teams');
  const u = new URL(seenUrl);
  assert.equal(u.host, 'pub-api-ro.fantasysports.yahoo.com');
  assert.equal(u.pathname, '/fantasy/v2/league/470.l.1/teams');
  assert.equal(u.searchParams.get('format'), 'json');
});

test('get sends the session cookie and a browser-like User-Agent', async () => {
  let seenInit;
  const client = createClient({
    cookieHeader: 'T=abc; Y=def',
    fetch: async (_u, init) => { seenInit = init; return okJson({ fantasy_content: {} })(); },
  });
  await client.get('league/470.l.1/teams');
  assert.equal(seenInit.headers.Cookie, 'T=abc; Y=def');
  assert.match(seenInit.headers['User-Agent'], /Mozilla/);
});

test('get returns normalized data, not the raw envelope', async () => {
  const raw = { fantasy_content: { 'xml:lang': 'en-US', league: [{ name: 'L' }, { teams: { 0: { team: [[{ team_key: 'k' }]] }, count: 1 } }] } };
  const client = createClient({ cookieHeader: 'c', fetch: okJson(raw) });
  const out = await client.get('league/470.l.1/teams');
  assert.equal(out.league.name, 'L');
  assert.deepEqual(out.league.teams, [{ team_key: 'k' }]);
  assert.equal(out['xml:lang'], undefined, 'metadata keys stripped');
});

test('get throws SessionExpiredError on 401 so the CLI can re-authenticate', async () => {
  const client = createClient({
    cookieHeader: 'c',
    fetch: async () => ({ ok: false, status: 401, text: async () => 'unauthorized' }),
  });
  await assert.rejects(() => client.get('league/470.l.1/teams'), SessionExpiredError);
});

test('get throws SessionExpiredError on 403 as well', async () => {
  const client = createClient({
    cookieHeader: 'c',
    fetch: async () => ({ ok: false, status: 403, text: async () => 'forbidden' }),
  });
  await assert.rejects(() => client.get('x'), SessionExpiredError);
});

test('get surfaces a Yahoo error envelope as YahooApiError with its description', async () => {
  const client = createClient({
    cookieHeader: 'c',
    fetch: okJson({ error: { description: 'Invalid team resource notathing requested.' } }),
  });
  await assert.rejects(() => client.get('team/x/notathing'), (e) => {
    assert.ok(e instanceof YahooApiError);
    assert.match(e.message, /Invalid team resource/);
    return true;
  });
});

test('get surfaces Yahoo\'s own description on a non-OK status', async () => {
  // Yahoo explains 400s in the body: "Invalid week 99 specified in the URI."
  // Discarding that leaves the user with a bare status code.
  const client = createClient({
    cookieHeader: 'c',
    fetch: async () => ({
      ok: false, status: 400,
      text: async () => JSON.stringify({ error: { description: 'Invalid week 99 specified in the URI.' } }),
    }),
  });
  await assert.rejects(() => client.get('team/x/roster;week=99'), (e) => {
    assert.ok(e instanceof YahooApiError);
    assert.match(e.message, /Invalid week 99/);
    return true;
  });
});

test('get falls back to the status code when a non-OK body is not JSON', async () => {
  const client = createClient({
    cookieHeader: 'c',
    fetch: async () => ({ ok: false, status: 500, text: async () => '<html>oops</html>' }),
  });
  await assert.rejects(() => client.get('x'), (e) => {
    assert.match(e.message, /500/);
    return true;
  });
});
