# Phase 2 findings — endpoint harvest  (2026-08-31)

STATUS: **PASS, and better than the plan assumed.**

## Headline
`pub-api-ro.fantasysports.yahoo.com/fantasy/v2/...` serves the **official Yahoo
Fantasy Sports API v2** — same URL grammar, same `fantasy_content` envelope —
authenticated by **session cookies alone**. No OAuth, no approved app, no browser
in the request path. Verified with `curl` only, 9/9 resources HTTP 200:

  settings, standings, teams, scoreboard, transactions, draftresults,
  players (paged), team roster, game stat_categories

Identity: `game_key=470` (NFL 2026), `league_key=470.l.1433971`.

## Consequence for the project
The OAuth wait is **not** on the critical path for reads. Scraping is not needed
either -- no DOM parsing, no HTML brittleness. The architecture is:

  browser (ONCE, for login)  ->  cookie jar  ->  plain HTTP against v2 API

The browser is an auth device, not a data path.

## Write path (found, not yet exercised)
- `pub-api.fantasysports.yahoo.com/fantasy/v3/getCrumb`  -> `{"service":{"crumb":"..."}}`
- `user-services.sports.yahoo.com/api/v1/gql/crumb`      -> `{"data":{"crumb":"..."}}`
- Note `pub-api` (no `-ro`) exists alongside `pub-api-ro`. The `-ro` split plus a
  crumb endpoint is the shape of a write path. UNTESTED -- phase 3.

## Other endpoints of interest
- `api-fantasy-mobile.sports.yahoo.com/v2/getPreDraftView?leagueKey=` — mobile app API
- `sports.yahoo.com/api/fantasy/teams/` — aggregate: my_league_keys, matchups_by_league_key,
  teams, team_recaps, records, playoff_matchup_slots
- `graphite.sports.yahoo.com/v1/query/shangrila/<query>` — GraphQL sports content (114KB)
- `sports.yahoo.com/api/fantasy/?gameCodes=&format=` — public fantasy content

## Method notes
- 170 endpoints captured; 141 were `s.yimg.com`/`sp.yimg.com` static JS (filter noise
  from allowing `content-type: javascript`). Only 29 were non-CDN.
- Yahoo Fantasy 2026 does NOT use `root.App.main` / `__PRELOADED_STATE__`.
  Pages are server-rendered; the data lives behind the pub-api hosts.
- `.yimg.com` should be denied outright in any future harvest.

## SECURITY
`cookies.txt` and `captures/` contain a LIVE session and personal data
(Yahoo GUID, display name, contacts). chmod 600. Never commit. Delete when done.
