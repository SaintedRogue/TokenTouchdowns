# Phase 1 findings — Yahoo session viability  (2026-08-31)

STATUS: **PASS.** All four questions answered affirmatively.

| Question | Answer | Evidence |
|---|---|---|
| Does Google accept an automated browser? | **Yes** | Chrome for Testing 151 + `ignoreDefaultArgs:['--enable-automation']` + `--disable-blink-features=AutomationControlled`. No "browser may not be secure". |
| Does the session persist to disk? | **Yes** | `T,Y,F,PH` on `.yahoo.com` in profile `Default/Cookies`, survived SIGTERM. |
| Does it survive a cold restart? | **Yes** | `verify.mjs` fresh process -> `authenticated`. |
| Does it work HEADLESS? | **Yes** | `verify.mjs --headless` -> `authenticated`. No window needed. |

## Session model
- Auth is **cookie-only**. 38 localStorage keys, all adtech; no tokens.
- Session cookies: `T` (secure+httpOnly), `Y` (secure), `F` (httpOnly), `PH`.
- Nominal expiry **365 days** (2027-08-31). NOT a guarantee: Yahoo may
  invalidate server-side sooner. Treat as an upper bound, detect-and-reauth.

## Captured for phase 2
- League ID: `1433971`
- `baseline-anon.json` — pre-login cookie state, for diffing.
- `adtech-denylist.json` — 149 non-Yahoo domains to filter from network capture.

## Detector notes (two bugs found and fixed, 9 regression tests)
1. `[data-ylk*="account"]` matches the signed-OUT ybar -> false positive.
2. Signed-in pages carry MORE `login.yahoo.com` links than signed-out ones
   (Account Info etc). `signInLinks>0` as a veto -> false negative.
   Fixed by requiring a real sign-in affordance + evidence scoring.
- `A1/A1S/A3` are ambient ad/consent IDs set BEFORE login. Never auth proof.

## Operational
- Profile: `~/.tokentouchdowns/browser-profile` (chmod 700). Contains LIVE
  Google + Yahoo credentials. Never commit; never copy into the repo.
- Avoid `waitUntil:'networkidle'` on Yahoo — ad calls never settle.
