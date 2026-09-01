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
actually scored** — never on this engine's own projections.

An earlier version of this section reported a larger edge (+45.1, 10/12 cells) for
VOR-with-survival. Those numbers were produced before three confirmed defects were fixed
(retired players on the board, a flex slot that was silently rounded away, and unseeded boards
that made the run irreproducible). They are superseded by the table below, which is smaller and
less favourable. Nothing was tuned in either direction.

### Methodology

Three independent backtest seasons (2023, 2024, 2025). For each:

1. **Projections fit on seasons strictly before S** (2015..S-1). Verified, not assumed: projecting
   2024 from full 2015-2025 history versus history truncated to `< 2024` gives a maximum
   `|delta proj_points|` of **0.0** across all 1,609 rows.
2. **Drafted on season S's own preseason ADP** from Fantasy Football Calculator (`year=S`;
   4,576 / 906 / 718 drafts) — never the current board.
3. **Board restricted to players with at least one regular-season game in S-1.** Without this the
   board carried retired players and the VOR arm actually drafted Tom Brady and Rob Gronkowski in
   the 2024 backtest. S-1 is legitimately preseason information, so this introduces no lookahead.
4. **Graded on actual season-S fantasy points** from nflverse weekly stats under this league's own
   scoring weights, restricted to `season_type == 'REG'`. Verified by hand: Lamar Jackson's 2024
   recomputes to 432.38 REG points, versus 474.54 if postseason had leaked in.
5. **A drafted player with no stats that season scores ZERO**, not NaN and not excluded. Dropping
   busts would flatter precisely the strategies that reach for them.
6. **Optimal starting lineup**: every fixed slot filled first, then the flex awarded to the best
   remaining flex-eligible player — 9 starters, not 8. Bench players do not score.
7. 200 trials per cell, 15 rounds, `my_slot = teams // 2`, opponents drafting ADP-with-noise.
   Boards are built exactly once per cell with a fixed seed, so the run is reproducible.

Player identity resolves through the tested crosswalk (FFC --fuzzy name+position--> Sleeper
--`gsis_id`--> nflverse), matching **91-93%** of each ADP board. An earlier exact-name join matched
only 16%, starving the survival signal on 60% of picks.

### Results — mean delta vs consensus ADP (actual points)

| season | teams | pure VOR | VOR x survival (uncond.) | VOR x survival (cond.) |
|--------|-------|----------|--------------------------|------------------------|
| 2023 | 4  | -120.5 **sig** | -10.8 ns | -29.1 ns |
| 2023 | 6  | -215.0 **sig** | +4.6 ns | +21.2 ns |
| 2023 | 8  | -232.0 **sig** | +22.3 ns | +20.9 ns |
| 2023 | 10 | -238.5 **sig** | +6.7 ns | -4.3 ns |
| 2024 | 4  | -125.3 **sig** | +55.0 **sig** | +58.5 **sig** |
| 2024 | 6  | -160.3 **sig** | +99.7 **sig** | +100.0 **sig** |
| 2024 | 8  | -355.2 **sig** | +71.3 **sig** | +81.6 **sig** |
| 2024 | 10 | -325.1 **sig** | +106.8 **sig** | +107.9 **sig** |
| 2025 | 4  | -328.6 **sig** | +12.8 ns | +16.0 ns |
| 2025 | 6  | -352.3 **sig** | **-91.8 sig** | **-79.8 sig** |
| 2025 | 8  | -314.2 **sig** | -21.4 ns | -18.4 ns |
| 2025 | 10 | -366.0 **sig** | +37.2 ns | +39.2 ns |

"sig" = the strategy's 95% CI does not overlap ADP's. ADP scores 1482 / 1548 / 1526 points per
season, so +24 is about a 1.6% difference.

| strategy | mean delta | beat ADP | significant wins | significant losses |
|----------|-----------|----------|------------------|--------------------|
| pure VOR | **-261.1** | **0/12** | 0 | **12** |
| VOR x survival (uncond.) | +24.4 | 9/12 | 4 | 1 |
| VOR x survival (cond.) | +26.2 | 8/12 | 4 | 1 |

### What this establishes

**Pure VOR is unusable — this is the finding.** It lost every one of the twelve cells,
significantly, by an average of 261 points. It also survived every attempt to break it: it loses
at every draft slot (-145 to -368), at every opponent-noise setting (-129 to -393), against a
heterogeneous field, and after retirees are removed. Each independent methodology fix made it
*worse*, which is how a real effect behaves. **Do not ship a VOR-ranked board.**

**The mechanism is bust rate.** Share of drafted players who scored zero, across all 36,000 picks:

| strategy | bust rate |
|----------|-----------|
| ADP | 0.9% |
| pure VOR | 4.4% |
| VOR x survival | 3.6-3.9% |

Pure VOR drafts non-appearing players at nearly **5x** the ADP rate. Consensus ADP embeds injury
and depth-chart information these projections do not model, so ranking on projected value alone
means repeatedly reaching for players who never take a snap.

**Conditional vs unconditional survival does not matter.** The two differ by 1.8 points on average
and every head-to-head confidence interval overlaps. The statistically-correct conditional form
(the board only holds players observed still available, so `P(X>next | X>pick)`) is not measurably
better than the simpler marginal one. The default stays `conditional=False` for numerical
stability, not performance.

### What this does NOT establish

**VOR-with-survival does not reliably beat consensus ADP.** The +24 average is carried entirely by
one season:

| season | mean delta | significant |
|--------|-----------|-------------|
| 2023 | +5.7 | 0/4 |
| 2024 | **+83.2** | **4/4 wins** |
| 2025 | -15.8 | 1/4 **losses** |

2024 is a strong, consistent win. 2023 is indistinguishable from ADP. 2025 is negative and
includes a significant loss. On this evidence the honest claim is that the survival-weighted board
**roughly matches consensus ADP**, with an edge that appears in some seasons and not others. It is
not a reliable per-draft advantage.

Further limits, each measured rather than asserted:

- **Draft slot matters more than the effect does.** 2023/10 by slot spans -37.0 to +72.2;
  2024/10 spans +9.6 to +154.8 — a 110-145 point range against a +24 headline, with the reported
  slot (`teams // 2`) at the favourable end in both cells sampled.
- **The edge flips sign on an unfitted constant.** With `ADP_NOISE_DEFAULT = 6.0`, 2023/10 gives
  +41.3; at noise 2 it is **-18.6**, at noise 15 it is +82.8, and against a heterogeneous top-3
  field it is **-15.1**. Opponents also share a single noise vector, so they deviate in perfect
  correlation, and the strategy under test models exactly the simulator's data-generating process.
  Pure VOR loses under every one of these settings.
- **The survival arm's advantage is partly hygiene, not insight.** Players absent from the ADP feed
  get `p_gone = 0`, so the survival arm automatically avoids them — which is how it dodged the
  retirees that pure VOR drafted.
- **The significance rule is conservative.** Non-overlapping CIs is roughly a 2x stricter test than
  a paired comparison on the same trials; the paired test would call additional cells significant.
  This under-claims rather than over-claims.
- One draft slot per cell; K and DEF are not projected at all; `Ret TD` and `2-PT` are unmodelled
  (surfaced as a warning via `league.missing_scored_columns`); three seasons is a small sample.
