import { test } from 'node:test';
import assert from 'node:assert/strict';
import { cookiesToHeader, isInteractive, SESSION_COOKIES } from '../src/session.js';

test('cookiesToHeader emits name=value pairs joined for a Cookie header', () => {
  const header = cookiesToHeader([
    { name: 'T', value: 'abc', domain: '.yahoo.com' },
    { name: 'Y', value: 'def', domain: '.yahoo.com' },
  ]);
  assert.equal(header, 'T=abc; Y=def');
});

test('cookiesToHeader keeps only yahoo cookies, dropping adtech noise', () => {
  // A real profile carries 800+ cookies from ~150 ad networks.
  const header = cookiesToHeader([
    { name: 'T', value: 'abc', domain: '.yahoo.com' },
    { name: 'uuid2', value: 'junk', domain: '.adnxs.com' },
    { name: 'KRTBCOOKIE_9', value: 'junk', domain: '.pubmatic.com' },
  ]);
  assert.equal(header, 'T=abc');
});

test('SESSION_COOKIES names the cookies that actually prove a session', () => {
  // A1/A1S/A3 are ambient ad+consent IDs set BEFORE login -- never proof.
  for (const ambient of ['A1', 'A1S', 'A3']) {
    assert.ok(!SESSION_COOKIES.includes(ambient), `${ambient} must not count as proof`);
  }
  for (const proof of ['T', 'Y']) assert.ok(SESSION_COOKIES.includes(proof));
});

test('isInteractive is false without a display even when a TTY is present', () => {
  // SSH without X forwarding: TTY yes, display no. Must not launch a browser.
  assert.equal(isInteractive({ isTTY: true, env: {} }), false);
});

test('isInteractive is false in CI even with a TTY and a display', () => {
  assert.equal(isInteractive({ isTTY: true, env: { DISPLAY: ':1', CI: 'true' } }), false);
});

test('isInteractive is false when explicitly suppressed', () => {
  assert.equal(
    isInteractive({ isTTY: true, env: { DISPLAY: ':1', TT_NO_INTERACTIVE: '1' } }), false);
});

test('isInteractive is true on a real desktop session', () => {
  assert.equal(isInteractive({ isTTY: true, env: { WAYLAND_DISPLAY: 'wayland-1' } }), true);
});
