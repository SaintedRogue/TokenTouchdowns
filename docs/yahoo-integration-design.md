# Yahoo Sports integration — architecture

**Status:** proposed
**Date:** 2026-08-31
**Supersedes:** the assumption that TokenTouchdowns must wait on Yahoo OAuth approval

---

## 1. Summary

TokenTouchdowns talks to Yahoo Fantasy through the **official Fantasy Sports v2
API**, authenticated by a **browser session** rather than OAuth. A real browser is
used exactly once — to log in — and never appears in the runtime data path.

```
  ┌──────────────┐
  │  Playwright  │   one-time, interactive, ~1×/year
  │   (login)    │
  └──────┬───────┘
         │  persists  T, Y, F, PH  cookies
         ▼
  ┌─────────────────────────────────────────────────────────┐
  │  plain HTTP client — no browser at runtime               │
  │                                                          │
  │  READ    GET  pub-api-ro/fantasy/v2/<resource>?format=json
  │  WRITE   PUT  pub-api-ro/fantasy/v2/<resource>            │
  │                ?format=json&crumb=<crumb>                 │
  │  CRUMB   GET  pub-api/fantasy/v3/getCrumb?format=json     │
  └─────────────────────────────────────────────────────────┘
```

**Consequences:** no HTML scraping, no DOM selectors, no headless browser per
invocation, no Playwright dependency at runtime. The data contract is Yahoo's
own documented API grammar, which is far more stable than their markup.

---

## 2. Why not OAuth

Yahoo's Fantasy Sports API is no longer self-serve. Applications are manually
reviewed, and — critically — **the granted access is read-only.**

That means OAuth cannot satisfy TokenTouchdowns' write requirements (set lineup,
add/drop) *at all*, no matter how long we wait. A session-based path is therefore
not a stopgap; it is the only mechanism that can ever perform a write.

The OAuth application should still be pursued. When granted it becomes the
preferred **read** transport — sanctioned, documented, rate-limited, with no
session to maintain — while writes continue over the session path.

---

## 3. Host map

Four Yahoo hosts, four different auth contracts. This table is the single most
expensive thing to re-derive; treat it as the reference.

| Host | Serves | Auth | Writes |
|---|---|---|---|
| `pub-api-ro.fantasysports.yahoo.com` | v2 resource API | session cookies | **yes** — `?crumb=` in query |
| `pub-api.fantasysports.yahoo.com` | v3 *service* API (`getCrumb`, `subscriptions`). 404s on v2 | session cookies | n/a |
| `fantasysports.yahooapis.com` | official documented API | **OAuth only** (cookies → 401) | read-only grant |
| `football.fantasysports.yahoo.com` | web UI actions (`addplayerwatch`, …) | cookies + scoped `<scope>\|<token>` **form** crumb | yes, but crumb is browser-minted |

Two traps worth stating explicitly:

- **`pub-api-ro` is a misnomer.** Despite `-ro`, it accepts `PUT`/`POST`.
- **Crumb placement is host-specific and mutually incompatible.**
  `pub-api-ro` requires the crumb in the **query string** — `x-csrf-token`,
  `y-crumb`, `Yahoo-App-Crumb` and `.crumb=` all return `403`. The
  `user-services` host requires the opposite: an `x-csrf-token` **header**.

---

## 4. Components

Three layers with deliberately different reliability budgets. Reads are stateless
and cheap; writes are rare and stateful. They should not share a failure model.

### 4.1 `session` — credential acquisition and custody
- Owns the Playwright persistent profile (`browser-profile/`, mode `0700`).
- `login()` — interactive, headed, human completes Google SSO. Rare.
- `cookies()` — exports `T`, `Y`, `F`, `PH` for the HTTP client.
- `isAlive()` — cheap authenticated probe (`users;use_login=1/profile`).
- The **only** module that imports Playwright. Everything else is `fetch`.

### 4.2 `client` — read path
- Thin wrapper over `GET pub-api-ro/fantasy/v2/<resource>?format=json`.
- Verified resources: `settings`, `standings`, `teams`, `scoreboard`,
  `transactions`, `draftresults`, `players` (paged), `team/<key>/roster`,
  `game/nfl/stat_categories`, `users;use_login=1/games;game_keys=nfl/leagues`.
- **Response normalisation is mandatory.** Yahoo's v2 JSON is a hostile shape:
  objects keyed by numeric strings with a sibling `count`, and entity attributes
  split across an array of single-key dicts. Normalise at the boundary so this
  shape never leaks into application code.

### 4.3 `writer` — write path
- Fetches a crumb from `v3/getCrumb` (session-scoped, stable, cacheable).
- Issues `PUT`/`POST` to `pub-api-ro` with `?crumb=` and an **XML** body —
  note reads are JSON but writes are XML.
- Serial only. Never parallel. Writes are user-visible actions in a live league.

---

## 5. Session lifecycle

Observed: session cookies carry a **365-day** nominal expiry. That is an upper
bound, not a guarantee — password changes, security events and server-side reaping
all invalidate a session whose cookie still looks valid on disk.

Therefore the client must **detect** a dead session, never trust the expiry date.
Detection is unambiguous: an authenticated read returns `401`/`403` instead of
`fantasy_content`.

On detection the CLI is **context-aware** — see §9.1. It re-authenticates
interactively when it can, and fails cleanly when it cannot.

---

## 6. Security

The stored session is equivalent to a logged-in Yahoo account, including Mail and
account settings — not merely fantasy access. Handle accordingly.

- `browser-profile/` is mode `0700` and lives outside the repo.
- `.gitignore` excludes `browser-profile/`, `cookies.txt`, `captures/`, `.env*`.
- Never log cookie values. Redact `Cookie` and `Authorization` headers in any
  debug output; log header *names* only.
- Captured API responses contain personal data (Yahoo GUID, display name, and —
  observed during discovery — contact records from `ws-contacts.progrss.yahoo.com`).
  Treat any response cache as sensitive.

---

## 7. Rate limiting and conduct

Yahoo's ToS disallows automated access. The realistic exposure is account-level
action, not legal jeopardy; the mitigation is behaving like a client rather than
a crawler.

- Serialise requests. No parallel fan-out.
- Minimum ~1s between calls; ~2.5s between page-equivalent operations.
- Cache aggressively. League settings and stat categories are near-static;
  rosters change on a human timescale.
- Send a realistic `User-Agent` and a `Referer` matching the fantasy origin.
- Exponential backoff on any `429`/`5xx`. Never retry a write automatically.

---

## 8. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Yahoo closes the `pub-api-ro` cookie door | medium | reads break | keep OAuth application alive; it fully covers reads |
| Session invalidated early | medium | all access breaks | cheap `isAlive()` probe; clear re-auth path |
| Crumb semantics change | low | writes break | crumb fetched at runtime, never hardcoded |
| Account flagged for automation | low | account action | rate limits above; serial writes |
| v2 JSON shape changes | low | parse errors | normalise at boundary; contract tests on fixtures |

---

## 9. Open questions

### 9.1 Session-expiry policy — DECIDED

**Context-aware re-authentication.** When the session is dead, auto-launch the
headed login flow *only* if the process can actually show a browser and prompt a
human; otherwise fail fast with instructions.

```js
if (!(await session.isAlive())) {
  if (isInteractive()) {
    await session.login()      // headed; human completes Google SSO
    return retry(cmd)          // resume the original command
  }
  fail('session expired — run: tt login')   // exit non-zero
}
```

`isInteractive()` must be conservative — a false positive hangs a cron job until
timeout, which is the worst outcome. Require **all** of:
- `process.stdin.isTTY && process.stdout.isTTY`
- a display: `$DISPLAY` or `$WAYLAND_DISPLAY` set
- not CI: `$CI` unset
- not explicitly suppressed: no `--no-interactive` flag / `TT_NO_INTERACTIVE`

Known edge cases this correctly refuses: SSH without X forwarding (TTY present,
no display), containers (no TTY, no display), systemd timers (neither).

Rationale: auto-launch is the smoothest interactive experience, but a browser
popping out of a scheduled job is both surprising and useless — nobody is there
to complete the SSO. Failing fast with an actionable message is strictly better
in that context. The check is the whole design; when uncertain, prefer failing.

### 9.2 Write payload shape — UNVERIFIED
Authorization and crumb validation are **proven** (see §10). The XML body format
is **not** — verification was blocked by `draft_status=predraft` (empty roster, no
legal transactions). Must be confirmed after the **2026-09-09** draft before any
write feature ships.

---

### 9.3 Populated transaction shape — UNVERIFIED

`tt transactions` renders correctly against a real **empty** response
(`transactions: []`, this league is pre-draft) and against a hand-built
fixture, `test/fixtures/league-transactions-SYNTHETIC.json`.

No populated transaction response was obtainable: the account has exactly one
league, in one season, with zero transactions. The synthetic fixture follows
the collection/attribute-array conventions verified against real teams,
players and roster captures, and Yahoo's documented field names
(`transaction_key`, `transaction_id`, `type`, `status`, `timestamp`,
`players[].transaction_data`) -- but it is an informed reconstruction, not
evidence.

Replace it with a real capture after the 2026-09-09 draft and re-run the
tests before trusting the populated path. Specifically unconfirmed: whether
`transaction_data` is an object or an array (the code tolerates both), and
the exact `type` values for trades and commissioner actions.

## 10. Evidence

Read path — 9/9 resources `HTTP 200` with well-formed `fantasy_content`, via
`curl` and a cookie jar only, no browser.

Write path — crumb genuinely validated, three distinct states:

| Crumb | Response |
|---|---|
| correct | `400` — `PUT request expects an input XML with roster data` |
| omitted | `403` — `Missing crumb.` |
| wrong value | `403` — `Invalid crumb.` |
| no cookies | `403` — `Missing crumb.` |

Session — persists to disk, survives a browser restart, and works **headless** on
a cold start. Auth is cookie-only; no tokens in `localStorage`.

Identity: `game_key=470` (NFL 2026) · `league_key=470.l.1433971`.

Full discovery notes: [`docs/discovery/`](discovery/) — phase 1 (session), phase 2
(endpoint harvest), phase 3 (write path).
