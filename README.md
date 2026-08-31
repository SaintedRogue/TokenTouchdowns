# TokenTouchdowns

Yahoo fantasy football CLI.

Reads go through the **official Yahoo Fantasy v2 API**, authenticated with a
browser session rather than OAuth. A real browser is used once — to log in — and
never appears in the runtime data path. See
[`docs/yahoo-integration-design.md`](docs/yahoo-integration-design.md).

## Usage

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
