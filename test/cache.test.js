import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { readCache, writeCache, isStale } from '../src/cache.js';

const tmp = () => mkdtempSync(path.join(tmpdir(), 'tt-cache-'));

test('isStale is false inside the TTL window', () => {
  const now = 1_000_000_000_000;
  assert.equal(isStale(now - 60 * 60 * 1000, 24, now), false);
});

test('isStale is true past the TTL window', () => {
  const now = 1_000_000_000_000;
  assert.equal(isStale(now - 25 * 60 * 60 * 1000, 24, now), true);
});

test('isStale treats a non-finite ttl as stale rather than fresh forever', () => {
  // A missing ttl must force a refetch, not silently freeze the cache.
  const now = 1_000_000_000_000;
  assert.equal(isStale(now - 1000, undefined, now), true);
  assert.equal(isStale(now - 1000, NaN, now), true);
});

test('readCache returns null when nothing has been written', async () => {
  const dir = tmp();
  try {
    assert.equal(await readCache('nope', { dir, ttlHours: 24 }), null);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('writeCache then readCache round-trips the data', async () => {
  const dir = tmp();
  try {
    await writeCache('demo', { a: 1 }, { dir, now: 5000 });
    const got = await readCache('demo', { dir, ttlHours: 24, now: 6000 });
    assert.deepEqual(got.data, { a: 1 });
    assert.equal(got.fetchedAt, 5000);
    assert.equal(got.stale, false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('readCache still returns expired data, marked stale', async () => {
  // Stale data beats no data: enrichment degrades, it does not fail.
  const dir = tmp();
  try {
    await writeCache('demo', { a: 1 }, { dir, now: 0 });
    const got = await readCache('demo', { dir, ttlHours: 1, now: 2 * 60 * 60 * 1000 });
    assert.equal(got.stale, true);
    assert.deepEqual(got.data, { a: 1 });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('readCache returns null for corrupt cache content', async () => {
  const dir = tmp();
  try {
    const { writeFileSync } = await import('node:fs');
    writeFileSync(path.join(dir, 'broken.json'), '{not json');
    assert.equal(await readCache('broken', { dir, ttlHours: 24 }), null);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
