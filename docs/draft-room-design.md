# Live draft room (`tt draft-room`) — architecture

**Status:** approved 2026-09-01. Target: the 2026-09-09 draft.

## 1. Summary

A localhost web app used during the live draft. It tracks picks as they happen, keeps a
re-ranked board, and answers one question repeatedly: *who should I take right now?*

It advises; the human picks. There is no automated drafting.

## 2. Why a server at all

The engine is Python and the Yahoo client is Node, and both already exist. What is missing is
something that holds STATE across a two-hour draft and re-answers the recommendation question
every few seconds without a human re-running a command. That is a server's job.

`tt draft-room --teams=N --slot=K [--port 8787] [--poll 3]` starts it and prints a URL.

## 3. The decision that makes it real-time

Projections are the expensive step: Monte Carlo over ~1,100 players, ~4 s. **They do not change
during a draft.** Neither does VOR — replacement level is a function of the league's roster slots
and team count (`vor.replacement_levels`), not of who has been taken.

So the board is computed ONCE at startup. Per pick, only three cheap things recompute:

  - **survival** — `p_available(adp, stdev, my_next_pick)` moves as the pick number advances
  - **roster need** — which of my slots are still empty
  - **ranking** — `VOR x P(gone) x need urgency`

That is milliseconds, so a 3-second poll costs nothing. Re-projecting per pick would cost 4 s and
make the app useless.

## 4. Components

```
tt draft-room
  ├─ startup    Python engine -> projections + VOR + tier + ADP join   (once)
  ├─ poller     GET league/{key}/draftresults every `--poll` seconds
  ├─ state      available pool | my roster | current pick | my next pick
  └─ HTTP       GET  /              the page (one static file, no build step)
                GET  /api/state     board + recommendations + draft status
                POST /api/taken     manual override: mark a player drafted
                POST /api/undo      revert the last manual mark
```

### 4.1 Snake order

The highest-risk pure logic in the app, and the cheapest to get wrong. For `teams` teams and
slot `K` (1-based), the picks belonging to K are, per round `r` (0-based):

  - r even: `r*teams + K`
  - r odd:  `r*teams + (teams - K + 1)`

For teams=4, K=2 that is 2, 7, 10, 15, 18, ... Current pick is `len(draft_results) + 1`; my next
pick is the smallest of my picks that is >= the current pick.

### 4.2 Draft results parsing — the one unverified surface

`league/{key}/draftresults` returns `draft_results`, confirmed reachable, but the account has no
completed draft, so **the shape of an entry is unverified until draft day**. That is the single
largest risk in this build.

Parsing is therefore defensive and LOUD: an entry that does not yield a pick number, a team key
and a player key is not silently skipped. On a shape mismatch the UI raises a banner and the app
falls back to manual entry. It must never present a stale board as a live one.

Yahoo player keys map to engine `player_id` through the existing tested crosswalk
(`src/identity.js` -> Sleeper `gsis_id` -> nflverse, with the name-matching second pass). An
unresolved pick still removes the player from the AVAILABLE pool by Yahoo key, so an identity gap
degrades the recommendation rather than corrupting the pool.

## 5. Failure modes

Every failure must degrade to a usable board. An assistant that blanks at pick 14 is worse than
no assistant.

| failure | behaviour |
|---|---|
| `draft_results` shape differs | loud banner, fall back to manual, never a silent stale board |
| Yahoo poll fails or rate-limits | keep last good state, show "last updated Ns ago" in amber |
| Session expires mid-draft | banner with the re-login command; manual override keeps working |
| Python engine error at startup | fail loudly before the draft, not during it |
| Python engine error later | keep the last good board, show the error |
| Drafting off-platform | manual override is a first-class path, not a fallback |

## 6. Testing

- Snake math: hand-computed picks for several (teams, slot) pairs, including the wrap at both
  ends of a round. Mutation-verified.
- Yahoo `fetch` and the Python `spawn` are both injected, so the whole server is testable with no
  network and no subprocess.
- A recorded `draft_results` fixture drives poll -> state transitions.
- Manual override, undo, malformed payloads, and the stale-data path each get a test.
- The recommendation shown must equal what `draft.recommend` returns for the same state — the app
  is a view over the engine, never a second implementation of it.

## 7. Out of scope

- Automated drafting. It recommends; the human picks.
- In-season pages (lineup, trades, standings) — those are existing CLI commands.
- Deployment, auth, multi-user. It binds to localhost; Yahoo cookies never leave the machine.
