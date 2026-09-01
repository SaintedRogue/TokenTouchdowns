# Draft & decision engine (Phase 2) — architecture

**Status:** proposed
**Date:** 2026-08-31
**Builds on:** [`prediction-engine-design.md`](prediction-engine-design.md)

---

## 1. Summary

Phase 1 produced calibrated inputs: half-PPR scoring, point-in-time features, a
backtest harness, measured baselines, and a distributional composer. Phase 2 turns
those into decisions — a draft board, a live pick recommender, a mock-draft
simulator, and in-season lineup and playoff optimisers.

The draft is the near-term deliverable: **2026-09-09**, live, 60 seconds per pick.

## 2. League parameters (read from the API, not assumed)

Fetched from `league/470.l.1433971/settings` and verified:

```
Roster:   QB1 · RB2 · WR2 · TE1 · FLEX(W/R/T)1 · K1 · DEF1 · BN6 · IR2  = 15
Scoring:  Pass Yd 0.04 · Pass TD 4 · Int -1 · Rush Yd 0.1 · Rush TD 6
          Rec 0.5 · Rec Yd 0.1 · Rec TD 6          (half-PPR)
Draft:    live, 60s/pick        Playoffs: 4 teams, week 16
Waivers:  rolling (no FAAB)     Teams: 4 joined, max 10
```

**Scoring is derived from these settings, not hardcoded.** Phase 1's `HALF_PPR`
constant is replaced by a loader that reads `stat_modifiers` from the league, so
the engine is not married to one league. The constant becomes the fallback.

### 2.1 Team count is the highest-leverage unknown

Replacement level — and therefore every VOR number — is a direct function of
starting slots x teams. At 10 teams, 20 RBs start weekly and replacement is around
RB21. At 4 teams, 8 RBs start and replacement is around RB9, which makes RB10-20
nearly worthless while concentrating scarcity at the top of QB and TE.

The league shows 4 joined of a 10 maximum. Team count is therefore an explicit
parameter, defaulting to the count actually joined at draft time, and the mock
simulator reports strategy sensitivity across 4, 6, 8 and 10 so the ambiguity is
visible rather than silently assumed.

## 3. Components

### 3.1 `projections` — 2026 expected points
No 2026 games have been played, so projections come from prior-season volume plus
the market:
- Volume: per-player rolling career volume with regression to positional means,
  weighted toward recent seasons.
- Efficiency and TD rate: shrunk priors (Phase 1 `shrunk_rate`).
- Points: composed via Phase 1 `simulate_points`, giving a distribution per player.
- Market blend: ADP-implied value is a strong prior. The projection is a weighted
  blend, and the WEIGHT IS A PARAMETER so the model's disagreement with the market
  is visible rather than hidden.

The disagreements are the product: a player whose historical usage is far better
than their ADP is the actionable output.

### 3.2 `vor` — value over replacement and tiers
- Replacement level per position = `slots x teams`, with FLEX allocated to the
  position most likely to fill it.
- VOR = projected points - replacement points.
- Tiers = gaps in sorted VOR; a tier break is where waiting costs materially more.

### 3.3 `survival` — will they last until my next pick?
FFC supplies `adp`, `stdev`, `high` and `low` per player, which is exactly the
input for a survival model: `P(available at pick k)` from a normal (or empirical)
CDF over draft position.

### 3.4 `draft` — the recommender
The rule most tools get wrong: **do not take the highest VOR — take the highest VOR
you would LOSE.** Expected cost of waiting is
`VOR(player) x P(gone before my next pick)`, and the pick maximises that, subject
to roster-slot feasibility.

### 3.5 `mock` — simulation and strategy testing
A full draft simulator:
- N opponents drafting from ADP with configurable noise.
- Your strategy plugged in as a function.
- Many trials, reporting the resulting roster's projected points and its
  distribution.
- Strategy comparison: VOR-with-survival vs pure ADP vs pure VOR vs positional
  runs, across team counts and draft slots.

This is how the draft logic gets validated before it is trusted live. A strategy
that cannot beat "just follow ADP" in simulation should not be used at the draft.

### 3.6 `lineup` and `playoff` — in-season
- Lineup: `argmax` expected points over legal lineups, FLEX resolved last.
- Playoff: maximise `P(my total > their total)` by simulating both lineups. When a
  large underdog, prefer variance; when favoured, prefer floor. This is the one
  decision that inverts based on the opponent, and it is why Phase 1 produces
  distributions rather than point estimates.

## 4. CLI surface

```
tt draft board [--teams N] [--slot K]     ranked board with VOR, tier, ADP, survival
tt draft pick  [--roster ...]             what to take right now
tt mock [--trials N] [--teams N] [--strategy S]   simulate and compare
tt lineup [--week N]                      optimal lineup
tt playoff [--week N] [--opponent T]      variance-aware lineup
```

## 5. Testing

- Every optimiser gets a case with a hand-computable answer.
- The survival model is checked against FFC's own `high`/`low` range.
- Mock drafts are seeded and reproducible.
- **Strategy comparison is the real test**: VOR-with-survival must beat pure-ADP
  in simulation, and if it does not, that is a finding to report rather than a bug
  to hide.

## 6. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Team count wrong at draft | **high** | every VOR number shifts | explicit parameter; sensitivity reported |
| Projections have no 2026 signal | certain | pre-season uncertainty | blend with ADP; show disagreement |
| Overfitting strategy to the simulator | medium | looks good, drafts badly | opponents drafted from real ADP with noise; compare against ADP baseline |
| K and DEF modelled poorly | medium | two roster slots | drafted last by convention; low VOR either way |

## 7. Out of scope

- In-game or DFS optimisation.
- Automated drafting. The engine recommends; the human picks.
- Trade valuation (Phase 3).

## Measured strategy comparison

Backtested 2026-09-01. **Every number below is out-of-sample and graded on points players
actually scored** — not on this engine's own projections.

### Methodology

Three independent backtest seasons (2023, 2024, 2025). For each:

1. **Projections fit on seasons strictly before S** (2015..S-1). Drafting 2024 with a fit that has
   seen 2024 is lookahead bias, and would make the whole exercise circular.
2. **Drafted on season S's own preseason ADP** from Fantasy Football Calculator
   (`year=S`; 4,576 / 906 / 718 drafts respectively) — never the current board.
3. **Graded on actual season-S fantasy points**, computed from nflverse weekly stats under this
   league's own scoring weights, restricted to `season_type == 'REG'`. Postseason weeks are
   excluded: counting them rewards players on deep playoff runs, which is unrelated to draft skill.
4. **A drafted player with no stats that season scores ZERO**, not NaN and not excluded. Dropping
   busts would flatter precisely the strategies that reach for them.
5. Optimal starting lineup per `league.starters_per_team`; bench players do not score.
6. 200 trials per cell, 15 rounds, `my_slot = teams // 2`, opponents drafting ADP-with-noise.

Player identity is resolved through the tested crosswalk (FFC --fuzzy name+position-->
Sleeper --`gsis_id`--> nflverse), matching **91-93% of the ADP board** per season. An earlier
exact-name join matched only 16%, which starved the survival signal on 60% of picks and made every
comparison before this one uninterpretable.

### Results — mean delta vs consensus ADP (actual points)

| season | teams | pure VOR | VOR x survival (uncond.) | VOR x survival (cond.) |
|--------|-------|----------|--------------------------|------------------------|
| 2023 | 4  | -75.1 **sig** | +30.8 ns | +20.1 ns |
| 2023 | 6  | -174.2 **sig** | +9.5 ns | +14.1 ns |
| 2023 | 8  | -94.4 **sig** | +55.1 **sig** | +49.7 **sig** |
| 2023 | 10 | -178.2 **sig** | +8.5 ns | -4.2 ns |
| 2024 | 4  | -68.7 **sig** | +62.2 **sig** | +61.9 **sig** |
| 2024 | 6  | -93.1 **sig** | +97.4 **sig** | +102.4 **sig** |
| 2024 | 8  | -182.1 **sig** | +113.4 **sig** | +115.2 **sig** |
| 2024 | 10 | -321.0 **sig** | +124.7 **sig** | +118.2 **sig** |
| 2025 | 4  | -232.6 **sig** | +51.8 **sig** | +58.9 **sig** |
| 2025 | 6  | -309.3 **sig** | **-89.3 sig** | **-80.4 sig** |
| 2025 | 8  | -287.6 **sig** | -30.5 ns | -23.1 ns |
| 2025 | 10 | -301.3 **sig** | +107.4 **sig** | +109.6 **sig** |

"sig" = the strategy's 95% CI does not overlap ADP's. Baseline ADP scores 1283 / 1343 / 1319
points per season, so +45 is roughly a 3.4% edge.

| strategy | mean delta | beat ADP | significant wins | significant losses |
|----------|-----------|----------|------------------|--------------------|
| pure VOR | **-193.1** | **0/12** | 0 | **12** |
| VOR x survival (uncond.) | +45.1 | 10/12 | 7 | 1 |
| VOR x survival (cond.) | +45.2 | 9/12 | 7 | 1 |

### What this establishes

**Pure VOR is worse than doing nothing clever.** It lost every one of the twelve cells, all
significantly, by an average of 193 points. Drafting the value-over-replacement board directly is
materially worse than simply following consensus ADP. This is the single most actionable finding
here, and it is the opposite of what an in-sample comparison graded on `proj_points` reported.

**The survival term is what carries the signal.** The only difference between the two rows is the
`x P(gone before my next pick)` factor, and it is worth ~238 points. VOR supplies magnitude;
survival supplies urgency. Optimising value while ignoring opportunity cost underperforms the
market; combining them beats it.

**The bust rate explains the mechanism.** Share of drafted players who scored zero:

| strategy | bust rate |
|----------|-----------|
| ADP | 2.2% |
| pure VOR | 11.7% |
| VOR x survival | 6.1% |

Pure VOR drafts non-appearing players at **5x** the ADP rate. Consensus ADP embeds injury and
depth-chart information our projections do not model; ignoring it means repeatedly reaching for
players who never take a snap.

**Conditional vs unconditional survival does not matter.** Head to head, conditional won 7/12 with
**every** confidence interval overlapping, and the two means differ by 0.1 points (+45.2 vs +45.1).
The statistically-correct conditional form (the board only holds players observed available, so
`P(X>next | X>pick)`) is not measurably better than the simpler marginal one. The default stays
`conditional=False` on grounds of numerical stability, not performance. This settles a question
that had been argued from theory in both directions.

### What this does NOT establish

- **The edge is not reliable per-league.** VOR-with-survival lost significantly in 2025/6-team and
  was flat in 2025/8-team. 2024 was uniformly strong (+62 to +125, all significant); 2023 was
  positive but mostly insignificant; 2025 was genuinely mixed. Expect an edge on average, not in
  any particular draft.
- **Opponents here draft ADP-with-noise.** A strategy exploiting the knowledge that everyone else
  follows consensus may transfer poorly to a league of heterogeneous or adversarial drafters.
- **One draft slot per cell** (`teams // 2`). Slot sensitivity is untested.
- **Kickers and team defenses are not projected at all**, and `Ret TD` / `2-PT` are unmodelled
  (surfaced as a warning, per `league.missing_scored_columns`).
- Three seasons is a small sample for a claim about seasons.
