# Multi-source data enrichment — architecture

**Status:** proposed
**Date:** 2026-08-31
**Relates to:** [`yahoo-integration-design.md`](yahoo-integration-design.md)

---

## 1. Summary

Yahoo remains the league of record. External sources contribute **player-level
attributes** that are joined onto Yahoo players: ADP, injury status, historical
stats, and weekly projections.

```
  Yahoo player_key 470.p.26686
        │  split -> 26686
        ▼
  identity.js  ── Sleeper crosswalk (cached) ──▶ sleeper_id 1466
        │                                        gsis_id  00-0030506
        │                                        espn_id  15847
        ▼
  enrich.js  joins source records onto Yahoo players
        ▲
        │
  sources/{sleeper, sleeper-proj, nflverse, ffc}
```

No source module knows Yahoo exists. Each emits records keyed by its own
identifier; `identity.js` alone owns translation.

## 2. Verified sources

All four were probed live on 2026-08-31 before this document was written.

| Source | Provides | Join to Yahoo | Documented | Notes |
|---|---|---|---|---|
| Sleeper `/players/nfl` | identity, injury, depth chart | **exact** via `yahoo_id` | yes | 14.6 MB, 12,225 players, 6,750 with `yahoo_id` |
| nflverse releases | weekly/season stats | **exact** via `gsis_id` | yes | GitHub releases, CSV/parquet |
| Sleeper `/projections` | weekly projections | **exact**, via Sleeper `player_id` then the crosswalk | **NO** | undocumented endpoint |
| Fantasy Football Calculator | ADP | **fuzzy** (name+pos+team) | yes | no external IDs exposed |

Verification notes:
- Sleeper `yahoo_id` for Travis Kelce is `26686`, byte-identical to the Yahoo
  player id observed in `player_key` `470.p.26686`. The join is exact.
- Sleeper also carries `gsis_id`, the NFL's official identifier and nflverse's
  join key — so Sleeper bridges Yahoo and nflverse in one hop.
- Sleeper's docs state the players payload is ~5 MB. It is **14.6 MB**.
- FFC returned live 2026 ADP (1,884 drafts, window ending 2026-08-31).

### Constraints
- **Sleeper**: no auth, no key. Stay under 1000 calls/min or risk an IP block.
  Licensed **non-commercial**; commercial use requires a separate agreement.
- **Sleeper projections is undocumented.** Same risk class as `pub-api-ro` in the
  Yahoo design: it can change or vanish without notice. Any feature depending on
  it must degrade gracefully, never hard-fail.
- **nflverse** is a community dataset published on GitHub; treat availability as
  good but not guaranteed.

## 3. Source module interface

Every module under `src/sources/` exports exactly three things:

```js
export const meta = {
  name: 'sleeper',
  provides: ['identity', 'injury', 'depth'],  // capability names
  joinKey: 'yahoo_id',                        // 'yahoo_id' | 'gsis_id' | 'fuzzy'
  ttlHours: 24,
  documented: true,
  license: 'non-commercial',
};
export async function fetchRaw({ fetch, season, week });  // -> raw payload
export function normalize(raw);                           // -> Map<sourceKey, record>
```

`meta.provides` drives `--with=adp,injury` without hardcoding a source list.
`meta.documented` and `meta.license` keep the caveats in code rather than only in
prose. `fetchRaw` takes an injected `fetch`, matching `client.js`, so every source
is testable without network.

Adding a source (ESPN, PFR) means writing one module and registering it. No
existing source or the join layer changes.

## 4. Identity resolution

`identity.js` builds one table from the Sleeper payload:

```
yahoo_id -> { sleeper_id, gsis_id, espn_id, name, position, team }
```

Yahoo's `player_key` is `<game_key>.p.<player_id>`, so the Yahoo side is a string
split — no request needed. The table is cached; it is the single most valuable
artifact in this design and the only reason exact joins are possible.

### 4.1 The fuzzy join (FFC only)

FFC exposes no external identifier, so ADP must be matched on name.

**Policy: fail loudly, never guess.** A wrong ADP attached to a plausible player
is worse than a missing one, because it would be drafted on.

1. Normalise: lowercase, strip punctuation, strip suffixes (`jr`, `sr`, `ii`,
   `iii`, `iv`, `v`), collapse whitespace.
2. Key on `normalised_name|position`. Position **must** match.
3. Team is a tiebreaker only — preseason trades make FFC's team lag Yahoo's.
4. More than one candidate after those rules -> **unmatched**. Never pick one.
5. Team defenses (`DEF`) are matched by team abbreviation, not by name.

`tt sync` reports the match rate. A silent drop from ~95% to ~60% after a name
format change is the failure this reporting exists to catch.

#### Baseline match rate (2026-08-31)

Measured against live Yahoo and FFC feeds on 2026-08-31:

```
Overall:  top 25 -> 25/25    top 75 -> 75/75    top 150 -> 149/150 (99.3%)
By position (25 queried each):
  QB 24/25 · RB 25/25 · WR 25/25 · TE 18/25 · K 18/25 · DEF 19/25
```

`ambiguous` was 0 across every sample. Future degradation:
- A rise in `ambiguous` means FFC and Yahoo naming conventions have drifted
  apart; investigate name normalisation rules.
- A rise in `absent` at the **top** of the board (where FFC has full coverage)
  means something else broke; check FFC payload structure.

**TE, K and DEF misses are CORRECT behaviour.** FFC publishes roughly 19 tight
ends, 20 kickers and 20 defenses in total. Unmatched players in these positions
are genuinely absent from the ADP feed, not mismatched.

Team defenses required a special implementation path: Sleeper assigns no
`yahoo_id` to defenses, so they never appear in the crosswalk. Enrichment falls
back to Yahoo's `editorial_team_abbr` to match them by team abbreviation. Before
this fallback existed, DEF matched 0/25.

## 5. Cache and freshness

Plain JSON files under `~/.tokentouchdowns/cache/`, one per source, each with a
fetched-at timestamp. No database: the working set is small and a schema is not
yet earned. If querying outgrows files, migrating to SQLite touches only
`cache.js` — source modules are unaffected.

| Source | TTL | Rationale |
|---|---|---|
| Sleeper players | 24h | 14.6 MB; roster churn is daily at most |
| Sleeper projections | 6h in-season | changes with news |
| FFC ADP | 12h | aggregated over a rolling draft window |
| nflverse stats | 6h in-season, 7d otherwise | published post-game |

Commands read cache-first and never block on a cold network. A missing or stale
cache degrades to "enrichment unavailable" rather than failing the command —
`tt roster` must still work when Sleeper is down.

## 6. CLI surface

```
tt players --with=adp,injury      add ADP and injury columns
tt roster  --with=proj,injury     decorate your roster
tt sync [--force] [--source=ffc]  refresh caches, report match rates
tt sources                        registered sources, cache age, staleness
```

`--with` accepts capability names from `meta.provides`, not source names, so a
capability can change provider without changing the user's command.

## 7. Testing

Each source gets a captured fixture and its own `normalize` tests, following the
pattern that caught three real shape bugs in the Yahoo normaliser. Fixtures are
scrubbed and deterministic (see `tools/capture-fixtures.mjs`).

The fuzzy matcher gets adversarial cases, which is where the bugs will be:
suffixes (`Jr.`, `III`), `James Cook III` vs `James Cook`, D/ST entries, two
players sharing a name at different positions, and a player whose team differs
between sources.

## 8. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Sleeper projections endpoint changes | medium | projections break | undocumented by design; degrade gracefully, never hard-fail |
| FFC match rate silently degrades | medium | wrong/missing ADP | never guess on ambiguity; `tt sync` reports match rate |
| Sleeper rate limit / IP block | low | all enrichment breaks | cache-first, 24h TTL, single serial fetch |
| nflverse release layout changes | low | stats break | pin asset names; fail with a clear message |
| Yahoo id absent from Sleeper | low | that player unenriched | 6,750 of 12,225 carry one; Yahoo lists only fantasy-relevant players |

## 9. Open questions

### 9.1 Projection scoring format
Sleeper projections carry multiple scoring variants (`ppr`, `std`, `half_ppr`).
The league is `scoring_type: head` — the exact mapping to a Sleeper variant is
**unverified** and must be confirmed against league settings before projections
are displayed as authoritative.

### 9.2 In-season behaviour unverified
Every source was probed pre-season (2026-08-31). Weekly stats and projections
return structurally valid but empty or zeroed data. Shapes must be re-verified
after the 2026-09-09 draft and the first game week.

## 10. Out of scope

- Sleeper (or any other platform) as a **league** provider. Yahoo remains the
  league of record; this document covers player-attribute enrichment only.
- Writes to any external source. All sources here are read-only.
- Projections modelling. We surface published projections; we do not compute them.
