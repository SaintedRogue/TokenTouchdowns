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

/** Does this jar prove a real Yahoo session? Ambient A1/A1S/A3 do not count. */
export function hasSessionCookies(cookies) {
  return cookies.some((c) => c.domain.includes('yahoo') && SESSION_COOKIES.includes(c.name));
}

const FANTASY_HOME = 'https://football.fantasysports.yahoo.com/';

/**
 * Interactive one-time login. Opens a real browser and waits for the human to
 * complete Yahoo/Google SSO, then persists the session to the profile.
 */
export async function login({ profileDir = PROFILE_DIR, timeoutMs = 5 * 60 * 1000,
                              log = console.log } = {}) {
  const { chromium } = await import('playwright');
  const ctx = await chromium.launchPersistentContext(profileDir, {
    headless: false,
    viewport: null,
    // Google's sign-in blocks browsers advertising automation.
    ignoreDefaultArgs: ['--enable-automation'],
    args: ['--disable-blink-features=AutomationControlled'],
  });
  try {
    const page = ctx.pages()[0] ?? (await ctx.newPage());
    await page.goto(FANTASY_HOME, { waitUntil: 'domcontentloaded' }).catch(() => {});
    log('Sign in to Yahoo in the browser window...');
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (hasSessionCookies(await ctx.cookies())) {
        log('Signed in. Session saved.');
        return true;
      }
      await page.waitForTimeout(2000);
    }
    log('Timed out waiting for sign-in.');
    return false;
  } finally {
    await ctx.close();
  }
}
