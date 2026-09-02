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

### 4.1 Pick order — read it, do not compute it

**Superseded by a live capture on 2026-09-01.** A Yahoo mock draft was run specifically to
capture the real payload, and it showed that `draft_results` publishes **every pick slot for the
whole draft, from before the first pick** — 210 entries for a 14-team, 15-round draft. Each slot
carries its owning `team_key`, so the entire draft order is known up front:

    round 1: p1:t1   p2:t2  ... p14:t14
    round 2: p15:t14 p16:t13 ... p28:t1

So the app READS the order rather than modelling it. That is authoritative, and it survives
custom orders, third-round reversal, and any league setting this project does not model.

  - `currentPick` = the lowest pending pick number
  - `onTheClock`  = that slot's `team_key`
  - `myNextPick`  = the lowest pending slot whose `team_key` is mine

`myPicks`/`nextPick` (the snake formula below) remain as the FALLBACK for when `draft_results` is
empty or unreachable. Yahoo's published order beats our model of it, every time.

For teams `T` and slot `K` (1-based), round `r` (0-based): r even -> `r*T + K`; r odd ->
`r*T + (T - K + 1)`. For T=4, K=2: 2, 7, 10, 15. Verified against the captured order.

### 4.2 Draft results parsing — resolved by capture

A pick slot appears in one of two normal states, and **both are expected**:

    made:    {"pick":1,"round":1,"team_key":"...t.1","player_key":"470.p.40059"}
    pending: {"pick":2,"round":1,"team_key":"...t.2"}          <-- no player_key yet

Field names are exactly `pick`, `round`, `team_key`, `player_key`.

The original design treated a missing `player_key` as a malformation. Against the real payload
that produced **1 pick and 209 "malformed"** at pick 1, so the banner would have fired from the
first second of the draft and never stopped — training the user to ignore the single signal that
means "your board is lying to you". Parsing therefore returns three groups: `picks` (made),
`pending` (future slots), and `malformed` (genuinely unusable — missing `pick` or `team_key`).
**Only `malformed` raises the banner.**

Yahoo player keys map to engine `player_id` through the existing tested crosswalk
(`src/identity.js` -> Sleeper `gsis_id` -> nflverse, with the name-matching second pass). An
unresolved pick still removes the player from the AVAILABLE pool by Yahoo key, so an identity gap
degrades the recommendation rather than corrupting the pool.

Scrubbed captures live in `test/fixtures/` and drive the parser tests.

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
