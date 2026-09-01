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
