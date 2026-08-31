# Phase 3 findings — write path  (2026-08-31)

STATUS: **Mechanism mapped. Reads fully solved; writes need a live page.**

## Yahoo runs THREE distinct write mechanisms
| Surface | Host | Crumb location | Body |
|---|---|---|---|
| Account/settings | `user-services.sports.yahoo.com/api/v1/gql/user` | `x-csrf-token` **header** | JSON `{"query":"<op>","variables":{}}` |
| Fantasy web | `football.fantasysports.yahoo.com/f1/<lg>/<action>` | `crumb` **form field**, `<scope>\|<token>` | `application/x-www-form-urlencoded` |
| `pub-api` v3 | `pub-api.fantasysports.yahoo.com` | `getCrumb` -> `apis\|<redacted>` | UNTESTED |

## Captured verbatim (watch list add/remove, Travis Kelce pid=26686)
```
POST /f1/1433971/addplayerwatch
Content-Type: application/x-www-form-urlencoded
mid=4&apid=26686&crumb=playerwatch|<redacted>
-> 200 {"content":"","hash":"...","js":"","objects":[
        {"league_id":"1433971","player_id":"26686","status":"watching"}]}

POST /f1/1433971/deleteplayerswatch
mid=4&dpids[]=26686&crumb=playerwatch|<redacted>
```
- `mid` = manager id (4). `apid` scalar for add; `dpids[]` array for delete.
- SAME crumb served both -> reusable across an action family within a session.

## The blocker for browserless writes
The `playerwatch|...` crumb is NOT obtainable over plain HTTP:
- absent from server-rendered HTML (`grep -c playerwatch` = 0)
- absent after full React hydration (2 headless loads, 3 scoped crumbs found, none relevant)
- `getCrumb?scope=playerwatch` -> HTTP 400 (no scope parameter supported)
=> it is minted in JS closure state at interaction time.

Session-wide crumbs ARE fetchable and stable:
- `pub-api/fantasy/v3/getCrumb` -> `<redacted>`  (appears in HTML as `apis|<redacted>`)
- `user-services/api/v1/gql/crumb` -> `<redacted>` (matches the live `x-csrf-token`)

## Consequence
Writes on fantasy React surfaces require a live Playwright page (drive the UI, or
intercept the app's own fetch to lift the scoped crumb). Reads need no browser at all.
UNTESTED next step: whether `pub-api` v3 accepts lineup writes with the `apis` crumb,
which would make writes browserless too.

---

# ADDENDUM (post-test) — the browser-for-writes conclusion above is WRONG

`pub-api-ro` accepts WRITE verbs with cookie auth + an HTTP-fetched crumb.
The `-ro` suffix is a misnomer. No browser required for writes either.

## Working write contract
```
PUT|POST https://pub-api-ro.fantasysports.yahoo.com/fantasy/v2/<resource>
         ?format=json&crumb=<crumb>          <-- crumb MUST be in the QUERY STRING
Cookie: T,Y,F,PH   (session)
Content-Type: application/xml
Body: <?xml version="1.0"?><fantasy_content>...</fantasy_content>
```
Crumb source (plain HTTP, no browser):
`GET https://pub-api.fantasysports.yahoo.com/fantasy/v3/getCrumb?format=json`
-> `{"service":{"crumb":"<redacted>"}}`   (session-scoped, stable)

## Control matrix proving the crumb is really validated
| crumb state | response |
|---|---|
| correct   | 400 `PUT request expects an input XML with roster data` / `could not be parsed as XML` |
| omitted   | 403 `Missing crumb.` |
| wrong     | 403 `Invalid crumb.` |
| no cookies| 403 `Missing crumb.` |

Crumb placement matters: **query string only**. Headers (`x-csrf-token`,
`y-crumb`, `Yahoo-App-Crumb`) and `.crumb=` all -> 403.

## Host map (corrected)
| host | serves | auth |
|---|---|---|
| `pub-api-ro.fantasysports.yahoo.com` | v2 resource API, **reads AND writes** | cookies (+crumb for writes) |
| `pub-api.fantasysports.yahoo.com`    | v3 *service* API only (getCrumb, subscriptions); 404s on v2 | cookies |
| `fantasysports.yahooapis.com`        | official documented API | **OAuth only** — cookies -> 401 |
| `football.fantasysports.yahoo.com`   | web UI actions (addplayerwatch etc) | cookies + scoped `<scope>\|<tok>` form crumb |

NOT completed: an actual successful mutation. Blocked by `draft_status=predraft`
(empty roster, no transactions possible). Auth/crumb are proven; payload shape
remains unverified until after the 2026-09-09 draft.
