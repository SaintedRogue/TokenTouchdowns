import { homedir } from 'node:os';
import path from 'node:path';

/**
 * Cookies that only exist once a real Yahoo session is established.
 * Deliberately excludes A1/A1S/A3 -- those are ad/consent identifiers set for
 * anonymous visitors and are NOT proof of authentication.
 */
export const SESSION_COOKIES = ['T', 'Y', 'SSL', 'F', 'PH'];

export const PROFILE_DIR =
  process.env.TT_PROFILE || path.join(homedir(), '.tokentouchdowns', 'browser-profile');

/** Build a Cookie header from a Playwright cookie jar, discarding adtech noise. */
export function cookiesToHeader(cookies) {
  return cookies
    .filter((c) => c.domain.includes('yahoo'))
    .map((c) => `${c.name}=${c.value}`)
    .join('; ');
}

/**
 * Can we actually show a browser AND have a human complete SSO?
 * Deliberately conservative: a false positive hangs a scheduled job on an
 * invisible browser until timeout, which is far worse than failing fast.
 */
export function isInteractive({ isTTY = process.stdin.isTTY, env = process.env } = {}) {
  if (!isTTY) return false;
  if (env.CI) return false;
  if (env.TT_NO_INTERACTIVE) return false;
  return Boolean(env.DISPLAY || env.WAYLAND_DISPLAY);
}

/**
 * Export the stored session as a Cookie header.
 * Playwright is imported lazily so the read path never needs it installed.
 */
export async function loadCookieHeader({ profileDir = PROFILE_DIR } = {}) {
  const { chromium } = await import('playwright');
  const ctx = await chromium.launchPersistentContext(profileDir, { headless: true });
  try {
    return cookiesToHeader(await ctx.cookies());
  } finally {
    await ctx.close();
  }
}
