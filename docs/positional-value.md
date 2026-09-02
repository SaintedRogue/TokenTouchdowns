# Positional value: what to take, and when

Measured 2026-09-02 on the same out-of-sample instrument as
`docs/draft-engine-design.md`'s "Measured strategy comparison": projections
fit on seasons strictly before S, drafted on season S's own preseason ADP,
**graded on the points players actually scored in season S**, with a drafted
player who never appeared scoring zero. Seasons 2023, 2024, 2025. Seed
`2026`. 400 trials per cell.

Three seasons is a small sample. Where that matters, it is said so.

---

## The short version

**Read this if you are about to draft.**

1. **Fill your lineup.** In the real 4-team league, simply not finishing the draft
   with an empty QB or TE slot is worth **63-80 actual points** -- more than any
   positional strategy in this document. An ADP-following drafter who never checks
   ends up with no tight end in 44% of 4-team drafts (11 of 25, real 2024 board).
2. **Do not run Zero-RB.** It is the strongest result here: 11 losses in 12 cells,
   nine of them significant, **-85 points** on average, negative in all three seasons
   and getting worse each year. Forcing two receivers in rounds 1-2 (-48) is the same
   mistake in a milder form.
3. **Never reach for a tight end.** `early_te` was below the baseline in **all twelve
   cells**. `late_te` -- refusing to take one before round 8 -- costs nothing at all
   (-1.8, no significant result in either direction). In a 4-team league the TE cliff
   sits at pick ~76, past the end of a 60-pick draft; you cannot be late.
4. **Taking a quarterback by round 3 is the best-behaved positive bet**, but it is not
   proven: +11.4 mean, five significant wins, and the only arm with **zero significant
   losses**, yet it went negative in 2025. It buys 23-80 points at the QB slot. It
   matters much more at 10 teams (+19.8) than at 4 (+3.0).
5. **"RB-first" has the biggest headline (+23.6) and the least support.** All of it is
   2025; in 2023 it lost significantly. Read it as "don't avoid running backs", not
   "always open RB-RB".
6. **Where the cliffs are.** RB and WR each fall off a step after roughly the 12th
   player at the position -- around overall pick 25-30, which is round 3 in a 10-team
   league and round 7 in a 4-team one. TE falls off after the top 6, around pick 76.
   **QB has no cliff at all** -- it is a gentle slope from QB1 to QB24, and no step in
   it is distinguishable from noise.
7. **In a 4-team league, waiting almost never costs anything.** The largest per-round
   waiting cost measured anywhere in a 4-team draft is 20 points; most rounds are under
   10. At 10 teams the same numbers run 20-55. Positional urgency is mostly a 10-team
   problem.

Consensus ADP is a strong baseline. Six of the eight positional strategies tested lose
to it. The two that beat it do so by under 2% of a season, and each one flips sign in
one of the three seasons available.

---

## Q1. What `proj_points` and `vor` actually are

### `proj_points`: confirmed as a full-season point total. Two corrections.

End to end (`analytics/src/tt/projections.py`, `project_players`):

1. `season_volume` projects per-**game** carries, targets and pass attempts from
   seasons strictly before S, recency-weighted. "Per game" means per game *played* --
   nflverse only emits a row for a game a player appeared in.
2. It separately projects `proj_games`, expected games played, shrunk toward a
   positional prior.
3. Efficiency (yards per opportunity), touchdown rate, catch rate, interception rate
   and fumble rates are never extrapolated from a player's own history -- each is
   shrunk hard toward a positional prior, because the project's own measurement says
   they are functionally random (yards/carry autocorrelates at r=0.016).
4. Season volume = per-game rate x `proj_games`. 5,000 Monte Carlo seasons are drawn
   per stream (rushing, receiving, passing) as (opportunities, yards, touchdowns),
   with receptions, interceptions and fumbles drawn from those same opportunity
   counts so a big-usage season also gets more of everything.
5. Every sample is converted to points using **this league's own scoring weights**,
   not a hardcoded half-PPR.
6. `proj_points` is the **mean** of those 5,000 season point totals. `p10`/`p50`/
   `p90`/`sd` describe the same distribution.

So yes: a full-season point total, in this league's scoring, as a mean.

**Correction 1 -- it is not a 17-game total.** The season length is each player's own
`proj_games`, which across the three real boards runs 4.5 to **13.8**, median ~10.
Nobody is projected for 17 games. That is deliberate (availability is projected, not
assumed), but it means `proj_points` is an *availability-adjusted expected total*, not
a healthy-season ceiling. The two happen to reconcile at the top of the board, where
`proj_games` reaches 12-13:

| position | top-N by projection | projected mean | actually scored | gap |
|---|---|---|---|---|
| QB | 12 | 231.8 | 236.6 | -2% |
| RB | 24 | 171.5 | 183.3 | -6% |
| WR | 24 | 169.2 | 173.6 | -3% |
| TE | 12 | 114.7 | 117.4 | -2% |

Do not read `proj_points` as "if he plays all year." Read it as "what I expect this
player to bank, injuries included."

**Correction 2 -- two scored stats are missing from every projection.** This league
scores `Ret TD` (statId 15/49) and `2-PT` (statId 16); `projections.py` simulates
neither and warns about it on every run. This is *not* a grading mismatch -- the
actual-points grader uses the same weight set, so both sides omit them consistently --
and the magnitude is small (19 special-teams touchdowns league-wide in all of 2024).
But every projected total is a little low, and a return specialist is undervalued.

Kickers and team defenses are not projected at all.

### `vor`: confirmed as margin over replacement. One precise correction.

`vor.add_vor` sorts each position by `proj_points` and computes
`vor = proj_points - proj_points[rank N]`, where
`N = round(starters_per_team[position] x teams)`. **Verified, not assumed**: on all
three real boards, at teams=4 and teams=10, `vor` is exactly 0.0 at that rank for
QB, RB, WR and TE.

`starters_per_team` splits the W/R/T flex evenly across RB/WR/TE, so this league is
QB 1.0, RB 2.333, WR 2.333, TE 1.333 per team. Replacement ranks:

| teams | QB | RB | WR | TE |
|---|---|---|---|---|
| 4 (the real league) | 4 | 9 | 9 | 5 |
| 10 (its maximum) | 10 | 23 | 23 | 13 |

**Correction -- the "replacement player" is the last starter, not a waiver-wire
player.** `vor.py`'s docstring describes him as "the player I could get for free off
waivers." He is not: rank N is exactly the number of league-wide starting slots, so
the player at rank N is the *worst player who still starts somewhere*. The first
player actually available is rank N+1. Using N instead of N+1 makes VOR **smaller**
than a waiver definition would, unevenly across positions:

| teams | QB | RB | WR | TE |
|---|---|---|---|---|
| 4 | -11.2 | -1.1 | -5.2 | -5.2 |
| 10 | -3.6 | -1.1 | -0.9 | -2.5 |

It changes nothing below -- a one-rank definitional detail -- but "margin over the
best freely available player" is not what the code computes, and the docstring should
be corrected rather than the numbers reinterpreted.

Three more things that are true:

- The flex slot is split evenly three ways regardless of which position actually
  fills it. A documented simplification; the real split is not knowable pre-draft.
- Replacement level is computed **over whatever board it is handed**. In this study
  the board is restricted to players with at least one regular-season game in S-1, so
  "rank 23" means rank 23 among plausibly-active players.
- `vor` is NaN for positions this league does not start. It is never invented.

---

## Q2. Which positions score the most — raw, and over replacement

Two orderings of the same four positions, on the same boards. They disagree, and the
disagreement is the whole reason VOR exists.

### Raw points -- QB wins, and it is not close

Mean over 2023-2025. "Starter pool" is the top-N at that position where N is the
league-wide starting slots (the replacement ranks in Q1).

| position | best projected | starter pool, projected | best actual | starter pool, actual |
|---|---|---|---|---|
| **QB** | **278.4** | **265.6** | **403.2** | **372.7** |
| RB | 225.6 | 198.9 | 353.4 | 279.6 |
| WR | 226.8 | 195.5 | 327.9 | 244.9 |
| TE | 167.4 | 135.7 | 207.9 | 173.2 |

*(4-team replacement ranks. At 10 teams the starter pools deepen and every number
falls, but the ordering does not change: QB 238.0 / RB 173.0 / WR 170.4 / TE 112.8
projected.)*

This ordering is robust. It holds in every season, at every team count, on the
projections and on what actually happened.

*(The two "actual" columns re-rank players within a position by what they actually
scored, so they are the best case after the fact and are biased slightly high --
picking the season's winner is not something a drafter can do. They are here to show
that the ORDERING of positions survives contact with reality, not as a target.)*

### Value over replacement -- QB loses, everywhere

Same boards, same players, same season. Only the question changes.

| position | best VOR, 4 teams | best VOR, 10 teams | starter-pool mean VOR, 4 teams | 10 teams |
|---|---|---|---|---|
| **QB** | **32.0** (last) | **73.1** (last) | 19.3 (last) | 32.8 |
| RB | 47.1 | **87.1** (first) | 20.4 | **34.5** |
| WR | **54.6** (first) | 82.2 | **23.3** | 25.8 |
| TE | 53.2 | 77.9 | 21.5 | 23.3 |

The size of the flip, stated plainly:

- **At 4 teams**, the QB starter pool projects **36% more raw points** than the RB
  starter pool (265.6 vs 198.9) -- and **4% less value** over its own replacement
  (19.3 vs 20.4). The best QB's VOR is **41% below** the best WR's.
- **At 10 teams**, QB is **38% ahead** on raw points and **16% behind** RB on best VOR.

**Why.** There are 78 QBs on the 2025 board and only 10 of them start in a 10-team
league (4 in the real one). The 10th-best QB projects 205 points; the 23rd-best RB
projects 139. Nearly the entire QB scoring curve sits above nearly the entire RB
curve -- so "quarterbacks score a lot" is just as true of the quarterback you can have
for free as of the one you would spend a first-round pick on. Raw points measure the
position. VOR measures the *decision*.

### The honest caveat on this table

The projected VOR column is a reproducible property of the board -- anyone can rerun
`points_by_position` and get it. The *actual* VOR ordering across positions is **not**
established by three seasons. Measured without hindsight -- players taken in
consensus-ADP order, replacement estimated as a six-player band straddling the
replacement rank, graded on actual points -- the QB starter pool's actual VOR at 4
teams was **+131, +99, -40** in 2023, 2024, 2025. RB's over the same three seasons was
**-16, +60, +106**. The season-to-season swing is several times larger than the gaps
between positions, and the two positions do not even agree on which season was good.

So: raw-points ordering, confirmed. Raw-vs-VOR *reversal*, confirmed on the board.
"Which position has the most VOR in a given year", not answerable on this sample.
Whether acting on VOR's story pays is Q3, which is a simulation rather than a table.

One further caution: the projections shrink every rate toward a positional prior, so
the board's VOR spread across positions (73-87 at 10 teams) is narrower than what
real seasons produce. The board under-separates positions.

---

## Q3. When to draft each position

### How the race was run

Eight arms. Every one is the **same** base strategy -- take the best player left by
consensus ADP -- wrapped in a positional constraint. Nothing else differs: not the
ranking, not the opponents, not the seeds (common random numbers, so trial *i* is the
same simulated draft night for every arm).

| arm | rule |
|---|---|
| `bpa` | no constraint. The control. |
| `rb_first` | rounds 1 and 2 must be RB |
| `wr_first` | rounds 1 and 2 must be WR |
| `zero_rb` | no RB before round 5 |
| `early_qb` | a QB on the roster by the end of round 3 |
| `late_qb` | no QB before round 8 |
| `early_te` | a TE on the roster by the end of round 3 |
| `late_te` | no TE before round 8 |

12 cells (2023/2024/2025 x 4/6/8/10 teams), 400 trials each, 15 rounds, my slot =
`teams // 2`, opponents drafting consensus ADP plus noise. Graded on the optimal
starting lineup's **actual** season points; a drafted player who never appeared scores
zero. Seed 2026; the boards rebuild byte-identically from that seed (verified).

Every arm also carries a **runway guard** -- once the picks remaining equal the
mandatory starter slots still unfilled, the board is restricted to positions the
roster still owes. It is identical across arms and cannot favour any of them. Without
it a roster-blind ADP drafter finishes with no tight end in 11 of 25 four-team drafts
on the real 2024 board (and no QB in 2 of 25), and every "take a TE early" arm would
win on roster management rather than positional value. See Limits for what the guard
alone is worth.

### Result: delta vs BPA, all 12 cells (actual points)

| arm | 4t 2023 | 4t 2024 | 4t 2025 | 6t 2023 | 6t 2024 | 6t 2025 | 8t 2023 | 8t 2024 | 8t 2025 | 10t 2023 | 10t 2024 | 10t 2025 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `rb_first` | +6.4 | -1.2 | **+46.1** | -11.3 | +13.9 | **+78.3** | **-27.3** | **+21.1** | **+83.5** | **-40.6** | **+37.2** | **+77.5** |
| `early_qb` | +10.2 | +8.0 | -9.3 | **+31.7** | +19.2 | -26.3 | **+41.2** | **+31.6** | -29.3 | **+31.3** | **+50.9** | -22.6 |
| `late_te` | +6.9 | +3.7 | -1.4 | +2.8 | +0.4 | -15.3 | -0.3 | -7.8 | -6.0 | -3.7 | +3.0 | -3.9 |
| `late_qb` | **-36.3** | -17.4 | +1.0 | **-64.2** | -22.5 | +28.4 | **-40.7** | **-36.2** | +18.2 | -14.2 | **-58.1** | +22.0 |
| `early_te` | -23.6 | **-54.0** | -6.6 | -27.3 | -6.7 | -12.4 | **-31.8** | -5.5 | -21.4 | -23.0 | -21.3 | -3.5 |
| `wr_first` | -19.8 | -3.6 | **-84.1** | -8.2 | **-33.0** | **-104.9** | -5.3 | **-48.7** | **-103.9** | +4.1 | **-62.1** | **-105.5** |
| `zero_rb` | **-26.4** | **-65.9** | **-164.5** | +2.9 | **-84.7** | **-170.7** | -14.7 | **-83.7** | **-155.5** | -19.1 | **-89.4** | **-144.8** |

**Bold** = the arm's 95% CI does not overlap BPA's in that cell. That rule is roughly
twice as strict as a paired test on these common random numbers, so it under-claims
rather than over-claims. BPA itself scores 1416-1747 depending on cell, so 50 points
is about 3%.

### Pooled

| arm | mean delta | cells above BPA | significant wins | significant losses | worst cell | best cell |
|---|---|---|---|---|---|---|
| `rb_first` | **+23.6** | 8/12 | 5 | 2 | -40.6 | +83.5 |
| `early_qb` | **+11.4** | 8/12 | 5 | **0** | -29.3 | +50.9 |
| `bpa` | 0.0 | -- | -- | -- | -- | -- |
| `late_te` | -1.8 | 5/12 | 0 | 0 | -15.3 | +6.9 |
| `late_qb` | -18.3 | 4/12 | 0 | 5 | -64.2 | +28.4 |
| `early_te` | -19.8 | **0/12** | 0 | 2 | -54.0 | -3.5 |
| `wr_first` | -47.9 | 1/12 | 0 | 7 | -105.5 | +4.1 |
| `zero_rb` | **-84.7** | 1/12 | 0 | **9** | -170.7 | +2.9 |

By season (mean over the four team counts) -- this is where the honesty lives:

| arm | 2023 | 2024 | 2025 |
|---|---|---|---|
| `rb_first` | **-18.2** | +17.7 | **+71.3** |
| `early_qb` | **+28.6** | **+27.4** | **-21.9** |
| `late_qb` | -38.9 | -33.6 | **+17.4** |
| `early_te` | -26.4 | -21.9 | -11.0 |
| `late_te` | +1.4 | -0.2 | -6.7 |
| `wr_first` | -7.3 | -36.9 | -99.6 |
| `zero_rb` | -14.3 | -80.9 | -158.9 |

By team count (mean over the three seasons) -- the real league is the 4 column:

| arm | 4 | 6 | 8 | 10 |
|---|---|---|---|---|
| `rb_first` | +17.1 | +27.0 | +25.8 | +24.7 |
| `early_qb` | **+3.0** | +8.2 | +14.5 | **+19.8** |
| `late_te` | +3.1 | -4.0 | -4.7 | -1.5 |
| `late_qb` | -17.6 | -19.4 | -19.6 | -16.8 |
| `early_te` | -28.1 | -15.4 | -19.6 | -15.9 |
| `wr_first` | -35.8 | -48.7 | -52.7 | -54.5 |
| `zero_rb` | -85.6 | -84.1 | -84.6 | -84.4 |

Zero-RB is the only arm whose damage is flat across league size (-84 to -86 in all
four). Every positive effect shrinks as the league gets shallower.


### The arms really are different rosters

Mean positional counts per drafted 15-man roster, 10-team league, averaged over the
three seasons. `empty_slots` -- mandatory starter slots left unfilled -- is **0.00 for
every arm**, which is the runway guard doing its job.

| arm | QB | RB | WR | TE | empty slots |
|---|---|---|---|---|---|
| `bpa` | 2.71 | 4.49 | 5.52 | 2.29 | 0.00 |
| `rb_first` | 2.62 | **5.59** | 4.53 | 2.26 | 0.00 |
| `wr_first` | 2.59 | 3.71 | **6.49** | 2.21 | 0.00 |
| `zero_rb` | 2.88 | **3.16** | **6.67** | 2.29 | 0.00 |
| `early_qb` | **3.38** | 4.21 | 5.16 | 2.25 | 0.00 |
| `late_qb` | **1.90** | 4.77 | 5.95 | 2.37 | 0.00 |
| `early_te` | 2.60 | 4.22 | 5.16 | **3.02** | 0.00 |
| `late_te` | 2.75 | 4.64 | 5.76 | **1.85** | 0.00 |

`zero_rb` finishes with 3.16 running backs against BPA's 4.49 and `rb_first`'s 5.59 --
a 2.4-back spread. These are genuinely different teams, not eight relabelings of the
same draft.

### What this establishes

**Zero-RB is bad, and this is the finding.** It lost 11 of 12 cells, nine of them
significantly, by an average of **85 actual points** -- roughly 5% of a season. It was
negative in all three seasons and got worse each year (-14, -81, -159). It is the
largest, most consistent effect in the study, and it does not depend on any single
season the way every positive result here does.

**The same signal shows up three times.** `zero_rb` (no RB for four rounds, -85),
`wr_first` (two forced WRs, which is also two rounds of no RB, -48) and `rb_first`
(two forced RBs, +24) are three different views of one variable: early running-back
exposure. All three point the same way. They are not independent evidence -- same
three seasons, same boards -- but they are coherent, which a fluke usually is not.

**Waiting on a tight end costs nothing.** `late_te` is -1.8 over 12 cells with **zero
significant results in either direction**, the flattest arm in the study. Meanwhile
`early_te` was **below BPA in all twelve cells**. Reaching for a tight end is a
mistake; waiting for one is free. This is the same fact the TE cliff in Q4 reports.

**Consensus ADP is hard to beat.** Six of eight arms lose to it. The two that win, win
by 11-24 points on a ~1500-point season -- under 2%.

### What this does NOT establish

**RB-first is not established, despite the best headline.** Its +23.6 is carried
entirely by 2025 (+71.3), where it won all four cells significantly. In 2023 it was
**negative in three of four cells and significantly negative in two**. A strategy that
loses significantly in one season and wins significantly in another, on three seasons,
is a coin whose bias has not been measured. What the data supports is the weaker,
directional claim: **do not systematically avoid running backs.** It does not support
"always open RB-RB".

**Early-QB is not established either, but has the better risk profile.** +11.4 mean,
five significant wins, and -- alone among all eight arms -- **zero significant
losses in twelve cells**. But 2025 flipped it negative in all four cells. Two good
seasons and one bad one is not a result.

Its mechanism is at least clean and measurable. Mean actual points scored by the
arm's starting quarterback:

| | 2023 | 2024 | 2025 |
|---|---|---|---|
| 4 teams, BPA | 313 | 315 | 284 |
| 4 teams, early-QB | **353** | **383** | **307** |
| 10 teams, BPA | 295 | 280 | 300 |
| 10 teams, early-QB | **353** | **360** | **309** |
| 10 teams, late-QB | 248 | 216 | 279 |

Forcing the pick in round 3 buys 23-80 points at the one starting QB slot. The arm's
total gain is smaller than that (+31 at 10 teams in 2023 against a +58 QB gain), so
the round-3 running back or receiver it gave up cost about 27. Net positive in two
seasons of three. Note also the team-count gradient: **+3.0 at 4 teams, +19.8 at 10** --
in a 4-team league only four quarterbacks start, so BPA already lands a 284-315 point
QB around round 8 without trying. Reaching early buys much less.

**Nothing here transfers cleanly to the real 4-team league.** The 4-team column is the
weakest everywhere: `early_qb` +3.0, `rb_first` +17.1, and both non-significant in
most cells. The one effect that survives at 4 teams is the negative one -- `zero_rb`
-85.6 and `wr_first` -35.8.

---

## Q4. Where the value cliffs are

Two different questions live under "value cliff", and they have different answers:

- **Where does the board fall away?** A property of the position, measured once per
  season. Answered below.
- **What does waiting one turn cost me right now?** A property of your draft slot and
  the room, measured per round. That is the per-round table in Q3.

### Why this is reported in blocks of six, not rank by rank

With three seasons there is exactly one observation per (season, positional rank).
The rank-to-rank noise dwarfs any plausible single-rank cliff -- standard deviation of
actual points across the three seasons, averaged over ranks 1-24:

| QB | RB | WR | TE |
|---|---|---|---|
| 89.4 | 78.2 | 53.9 | 37.4 |

A rank-by-rank cliff table on this sample would be a picture of three seasons' luck. A
first pass at exactly that table nominated WR rank 25 and RB rank 14 as the biggest
drops, and also produced a 55-point *rise* at WR rank 28. Blocks of six average 18
observations and are where a real level change starts to separate from noise.

### Where each position actually falls away

Mean ACTUAL points, 2023-2025, by **preseason consensus ADP rank within the
position**. "Clear" means the drop cleared 1.96 standard errors.

| position | 1-6 | 7-12 | 13-18 | 19-24 | 25-30 | 31-36 | the one clear cliff |
|---|---|---|---|---|---|---|---|
| QB | 278.8 | 246.5 | 229.9 | 216.4 | - | - | **none** |
| RB | 241.9 | 217.4 | **149.4** | 162.1 | 127.3 | 90.0 | after RB12: **-68.0 ± 26.2** |
| WR | 213.5 | 191.9 | **150.2** | 145.7 | 147.5 | 133.7 | after WR12: **-41.7 ± 18.8** |
| TE | 144.4 | **106.6** | 83.3 | - | - | - | after TE6: **-37.8 ± 14.0** |

### What that means at the table

Translated from positional rank into the overall pick where that player typically
goes (mean consensus ADP, 2023-2025):

| position | cliff after | that player goes around pick | 4-team league | 10-team league |
|---|---|---|---|---|
| RB | RB12 | 27 | round 7 | round 3 |
| WR | WR12 | 25 | round 7 | round 3 |
| TE | TE6 | 76 | past the end of a 60-pick draft | round 8 |
| QB | -- | -- | -- | -- |

- **RB and WR each have exactly one cliff, and it is in the same place: around the
  12th player at the position.** Above it, RB and WR blocks average 218-242 and
  192-214. Below it, 149-162 and 146-150. That step is real and it is large -- 68
  points at RB is roughly four points a week for a whole season.
- **TE's cliff is much earlier and much shallower in absolute terms: after the top
  six.** 144 -> 107 is a 26% drop, the largest proportional step of any position, but
  it is 38 points, not 68.
- **QB has no cliff.** 279 -> 247 -> 230 -> 216 is a gentle slope, and no single step
  in it is distinguishable from noise. QB1 through QB24 is a ramp, not a staircase.
  This is the same fact Q2's VOR table reports, arriving from a different direction.

**In the real 4-team league, only the RB and WR cliffs are reachable at all.** A
4-team draft is 60 picks; TE6 typically goes at pick 76. There is no scenario in which
a 4-team drafter has to reach for a tight end.


---

## The per-round table: what to take now

The draft-day artifact. For each round: **if I take the best remaining player at this
position now instead of at my next turn, how many points is that worth?** Measured two
independent ways.

- **Actual** -- the player's real season points. Honest, out-of-sample, and noisy:
  three seasons is three observations per cell.
- **Projected** -- the same measure on the board's own `proj_points`. This is the
  number a live draft-room UI can actually compute, because it needs no knowledge of
  how the season turned out. Smooth, but it is the board's opinion of its own shape,
  not a validated forecast.

**Read the honest warning first: on three seasons, NOT ONE ROUND has a statistically
separable winner.** Between-season standard errors run 4-46 points against gaps of
0-31. The tables below are the best estimate available, not a settled ranking. Where
the two independent measures agree, that agreement is the strongest evidence there is.

### 10-team league, by round

Mean actual points lost by waiting one turn (2023-2025):

| round | QB | RB | WR | TE | best guess |
|---|---|---|---|---|---|
| 1 | -16.7 | -23.1 | 2.8 | 8.5 | TE (all tiny) |
| 2 | 0.3 | **40.9** | 25.1 | -5.5 | **RB** (positive all 3 seasons, 22..63) |
| 3 | 21.1 | 10.3 | 10.9 | -26.1 | QB |
| 4 | 25.5 | 18.5 | 7.2 | 9.0 | QB |
| 5 | 26.0 | -5.2 | 4.3 | 15.9 | QB |
| 6 | 24.1 | 11.2 | -15.0 | 21.1 | QB |
| 7 | -43.0 | **19.3** | -5.4 | 1.3 | **RB** (positive all 3, 7..39) |
| 8 | -4.9 | 15.8 | **31.6** | 21.9 | **WR** (positive all 3, 21..52) |
| 9 | 8.3 | -30.8 | -4.3 | -14.3 | QB |
| 10 | -22.7 | 6.0 | 19.4 | -30.4 | WR |
| 11 | 17.4 | 23.1 | -3.5 | **26.0** | **TE** (positive all 3, 10..57) |
| 12 | **54.6** | 23.2 | -20.3 | -2.4 | **QB** (positive all 3, 6..90) |
| 13 | -49.9 | -23.3 | **29.7** | -3.2 | **WR** (positive all 3, 3..56) |
| 14 | -8.3 | 16.2 | 11.5 | 9.8 | RB |

### 4-team league (the real one), by round

| round | QB | RB | WR | TE | best guess |
|---|---|---|---|---|---|
| 1 | -0.1 | 4.0 | -0.0 | 4.0 | tie, all tiny |
| 2 | -4.8 | -16.7 | 4.9 | 5.8 | TE (tiny) |
| 3 | -6.3 | -4.5 | -2.4 | 1.1 | nothing |
| 4 | -16.5 | **20.3** | 2.5 | 0.1 | **RB** (positive all 3, 11..32) |
| 5 | -0.9 | 8.5 | **9.4** | -0.9 | **WR** (positive all 3, 5..18) |
| 6 | **17.8** | 3.9 | 9.1 | -5.5 | **QB** (positive all 3, 6..36) |
| 7 | **8.8** | 2.6 | 4.1 | -7.0 | **QB** (positive all 3, 3..14) |
| 8 | 4.9 | 7.1 | 5.0 | -13.3 | RB |
| 9 | 6.7 | 7.6 | -5.0 | -2.9 | RB |
| 10 | 10.7 | 10.0 | 3.8 | 4.1 | QB |
| 11 | 6.7 | 0.5 | **10.7** | 5.2 | **WR** (positive all 3, 6..15) |
| 12 | 16.2 | -3.2 | -0.8 | 9.0 | QB |
| 13 | 2.8 | -2.3 | 4.1 | 2.8 | WR (tiny) |
| 14 | 12.3 | 2.4 | -3.1 | 9.7 | QB |

**The single most useful number in this table is how small the 4-team column is.** The
largest waiting cost anywhere in a 4-team draft is **20 points** (RB, round 4); most
rounds are under 10. The same table at 10 teams runs 20-55. A 4-team league drafts 60
players out of a 500-player pool -- almost nothing you want is ever gone by your next
turn. **Positional urgency is mostly a 10-team problem.**

### By draft phase, where the noise is smaller

Averaging three rounds at a time gives nine observations per cell instead of three.
Actual points, with the board's own projected version beside it:

| phase | 10 teams, actual | 10 teams, projected | agree? | 4 teams, actual | 4 teams, projected | agree? |
|---|---|---|---|---|---|---|
| rounds 1-3 | **WR** 12.9 | **WR** 11.7 | yes | TE 3.6 | WR 7.2 | no (all tiny) |
| rounds 4-6 | **QB** 25.2 | **QB** 19.3 | yes | RB 10.9 | WR 5.9 | no |
| rounds 7-9 | **WR** 7.3 | **WR** 9.7 | yes | QB 6.8 | TE 5.1 | no |
| rounds 10-12 | **RB** 17.5 | **RB** 5.4 | yes | **QB** 11.2 | **QB** 4.3 | yes |
| rounds 13-14 | WR 20.6 | QB 20.2 | no | **QB** 7.5 | **QB** 8.1 | yes |

At 10 teams the two independent measures pick the same position in **four of five
phases**. That is the closest thing to corroboration this sample can provide, and it
says: receivers early, quarterback in the middle rounds, running backs late. At 4
teams they agree in only two of five phases and every number is small -- consistent
with "urgency barely exists in a shallow league".

### Two things this table does not know

1. **It does not know your roster.** It answers "what does the board cost me", not
   "what do I need". A UI must intersect it with `draft.roster_need`; a drafter should
   read it beside their own roster. The round-12 QB number at 10 teams is real board
   shape and completely irrelevant if you already have two quarterbacks.
2. **It assumes you would take the consensus-best player at that position.** It is the
   cost of waiting, not the cost of waiting *and* picking badly.

---

## Limits

Each of these is measured, not asserted.

- **Three seasons.** Every "per season" number below is one observation. Where a
  difference is quoted with a between-season error bar, that bar is computed on n=3.
  A result that is consistent in sign across three seasons AND four team counts is
  the strongest claim this sample can support; it is not a proof.
- **One draft slot per cell** (`teams // 2`, a fixed, untuned middle-of-the-order
  choice). The existing backtest measured a 110-145 point spread across draft slots
  against a +24 headline effect -- draft slot matters more than most of these
  differences do.
- **The opponents are a model, not a league.** Every opposing team drafts consensus
  ADP plus a shared Gaussian noise draw (sd 6.0, an unfitted constant). They deviate
  from consensus in perfect correlation with each other, and none of them reacts to
  positional runs, which is exactly the behaviour a positional-timing strategy is
  supposed to exploit. A real room that stampedes on running backs would make
  RB-first look better and Zero-RB look worse than measured here.
- **The runway guard is doing real work.** Measured directly against a bare,
  unguarded ADP drafter on the same boards, seeds and grading:

  | season | 4 teams | 10 teams |
  |---|---|---|
  | 2023 | +63.2 **sig** | +33.3 **sig** |
  | 2024 | +79.5 **sig** | +22.6 ns |
  | 2025 | +64.5 **sig** | +0.5 ns |

  Positive in all six cells; significant in four. In the real 4-team league, simply
  not finishing the draft with an empty lineup slot is worth **63-80 actual points**
  -- larger than any positional-timing difference in Q3. The guard is applied
  identically to all eight arms so it cannot favour any of them, but it does mean
  these scores are not comparable to the unguarded `adp` arm in
  `docs/draft-engine-design.md`.
- **Ranking is held fixed at consensus ADP.** These arms answer "when should I take
  this position", not "who is the best player at it". A better within-position
  ranking would shift every arm, possibly unequally.
- **`proj_points` omits `Ret TD` and `2-PT`** (Q1, correction 2), and kickers and team
  defenses are not projected at all. K/DEF slots score zero in every arm equally.
- **The waiting-cost measure is board shape, not roster advice.** It answers "if I
  take the best remaining player at this position now instead of at my next turn, how
  many points is that worth" -- it does not know whether you already have three of
  them. Read it beside your own roster, not instead of it.
- **Nothing here was tuned.** The round numbers in each arm (RB-first = rounds 1-2,
  Zero-RB = no RB before round 5, early = by round 3, late = not before round 8) are
  the conventional definitions, chosen before any result was seen and not adjusted
  afterward.

---

## Reproducing this

From `analytics/`, with the gitignored data files in place:

```
.venv/bin/python scripts/run_positional.py        # ~65 min, caches every cell
.venv/bin/python scripts/analyse_positional.py    # prints every table above
```

Seed 2026 throughout. Boards rebuild byte-identically from that seed -- verified by
rebuilding the 2024 board and diffing against the cache (`max |delta proj_points|` =
0.0). Results land in `analytics/data/positional/` (gitignored; nothing here is
committed).

The engine code is `analytics/src/tt/studies/positional.py`, tested in
`analytics/tests/test_positional.py` (38 tests). Every test was mutation-verified:
removing the my-own-pick add-back from the waiting-cost measure, making the runway
guard fire every round, computing the replacement rank from rounded per-team starter
counts, letting an unsatisfiable constraint abort the draft, leaking a round window
past its round, and off-by-one on both `forbid_before` and `require_by` each turn a
test red.
