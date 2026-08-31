import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { homedir } from 'node:os';
import path from 'node:path';

export const CACHE_DIR =
  process.env.TT_CACHE_DIR || path.join(homedir(), '.tokentouchdowns', 'cache');

export function cachePath(name, dir = CACHE_DIR) {
  return path.join(dir, `${name}.json`);
}

export function isStale(fetchedAt, ttlHours, now = Date.now()) {
  return now - fetchedAt > ttlHours * 60 * 60 * 1000;
}

/**
 * Returns { data, fetchedAt, stale } or null when absent/corrupt.
 * Expired entries are returned WITH stale:true rather than discarded --
 * stale enrichment beats no enrichment.
 */
export async function readCache(name, { dir = CACHE_DIR, ttlHours, now = Date.now() } = {}) {
  try {
    const raw = await readFile(cachePath(name, dir), 'utf8');
    const parsed = JSON.parse(raw);
    if (typeof parsed?.fetchedAt !== 'number') return null;
    return { data: parsed.data, fetchedAt: parsed.fetchedAt,
             stale: isStale(parsed.fetchedAt, ttlHours, now) };
  } catch {
    return null;
  }
}

export async function writeCache(name, data, { dir = CACHE_DIR, now = Date.now() } = {}) {
  await mkdir(dir, { recursive: true });
  await writeFile(cachePath(name, dir), JSON.stringify({ fetchedAt: now, data }));
}
