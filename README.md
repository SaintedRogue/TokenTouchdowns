# TokenTouchdowns

Yahoo fantasy football CLI.

Reads go through the **official Yahoo Fantasy v2 API**, authenticated with a
browser session rather than OAuth. A real browser is used once — to log in — and
never appears in the runtime data path. See
[`docs/yahoo-integration-design.md`](docs/yahoo-integration-design.md).

## CLI

```sh
npm install
node bin/tt.js login        # one-time; opens a browser
node bin/tt.js leagues
node bin/tt.js teams        # league resolved from your leagues
node bin/tt.js standings 470.l.1433971
node bin/tt.js roster       # your team resolved automatically
node bin/tt.js matchup      # current week; `matchup 5` for a specific week
node bin/tt.js players --position=RB --status=A --count=10
node bin/tt.js players --search=kelce
node bin/tt.js transactions --type=add,drop
node bin/tt.js leagues --json
```

```
$ tt matchup
Week 1  (preevent)
   TEAM                      PTS   PROJ
*  Token Maxxing Touchdowns  0.00  0.00
   The Turing Test Dummies   0.00  0.00

$ tt players --position=RB --status=A --count=3
PLAYER            POS  TEAM  BYE  STATUS
Jahmyr Gibbs      RB   Det   6
Bijan Robinson    RB   Atl   11
Jonathan Taylor   RB   Ind   13
```

Player filters: `--position` `--status` `--search` `--sort` `--count` `--start`.
Results are sorted by overall rank unless `--sort` says otherwise.

Transaction filters: `--type=add,drop` `--team=<team_key>` `--count`.
One row per player movement, so an add/drop reads as two lines.

Exit codes: `0` ok · `1` usage error · `2` session expired · `3` Yahoo API error.
On an expired session the CLI re-authenticates when interactive, and otherwise
fails with `Run: tt login` — it never opens a browser in cron or a container.

## Library usage

```js
import { createClient } from './src/client.js';
import { loadCookieHeader } from './src/session.js';

const client = createClient({ cookieHeader: await loadCookieHeader() });

const { users } = await client.get('users;use_login=1/games;game_keys=nfl/leagues');
const { league } = await client.get('league/470.l.1433971/teams');
const { team }   = await client.get('team/470.l.1433971.t.4/roster');

league.teams.find((t) => t.is_owned_by_current_login === 1);
```

Responses are normalised: collections become arrays, Yahoo's attribute lists
become plain objects, and the `fantasy_content` envelope is stripped.

## Tests

```sh
npm test              # unit tests, no network
TT_LIVE=1 npm test    # also runs live API tests (needs a logged-in profile)
```

## Session

One-time login creates `~/.tokentouchdowns/browser-profile` (mode `0700`).
**That profile is equivalent to a logged-in Yahoo account — never commit it.**
On a dead session the client throws `SessionExpiredError`.
