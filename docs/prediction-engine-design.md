# Prediction engine — architecture

**Status:** proposed
**Date:** 2026-08-31
**Relates to:** [`multi-source-enrichment-design.md`](multi-source-enrichment-design.md)

---

## 1. Summary

A component-wise fantasy football projection pipeline. Historical outcomes come
from nflverse, joined to Yahoo players through the Sleeper crosswalk already
built. Predictions are **distributions**, not point estimates, produced by
modelling the parts of performance that are actually predictable and regressing
the parts that are not.

```
  nflverse            Yahoo v2         Sleeper        FFC
  weekly stats        league state     identity       ADP
  schedules           (existing)       (existing)     (existing)
      |                    |________________|____________|
      v                                     |
  ingest/   python + duckdb -> parquet      | node (existing, untouched)
      v                                     |
  features/  POINT-IN-TIME correct          |
      v                                     |
  backtest/  walk-forward, vs baselines     |  <- built BEFORE any model
      v                                     |
  models/    volume | efficiency prior | TD rate
      v                                     |
  decide/    draft . lineup . waiver . playoff  <- consumed by the tt CLI
```

## 2. The empirical basis

Measured on nflverse 2025 regular season (19,422 player-weeks) before this
document was written. These numbers determine the architecture.

### 2.1 Week-to-week autocorrelation (lag-1, RB/WR/TE)

| Quantity | r |
|---|---|
| carries | **0.825** |
| targets | **0.623** |
| fantasy_points_ppr | 0.466 |
| touchdowns | 0.147 |
| yards per target | 0.046 |
| yards per carry | 0.016 |

**Fantasy points are less predictable than their own inputs.** Aggregating to
points before predicting fuses a highly autocorrelated quantity (volume) with
noise (efficiency), discarding signal. Hence component-wise modelling.

**Efficiency is functionally random**, not weakly predictable. It must be
regressed to a prior, never used as a predictor.

### 2.2 Defence-vs-position split-half reliability (wk 1-9 vs 10-18, 32 teams)

| Position faced | r |
|---|---|
| QB | 0.167 |
| WR | 0.142 |
| RB | **-0.053** |
| TE | **-0.088** |

Points allowed by position largely measures **which offences a team happened to
play**, not defensive quality. Opponent adjustment therefore earns a small,
heavily-regressed term for QB and WR only, and is excluded for RB and TE.

CAVEAT: single season, 32 teams per split. Direction matches published work, but
replication across 2015-2025 is the backtest harness's first task (see §9.1).

### 2.3 Usage metric stability (WR/TE, lag-1)

`target_share` 0.656 · `wopr` 0.646 · `targets` 0.610. Shares edge raw counts and
normalise for team context, so they survive a player changing teams.

## 3. Substrate

- **DuckDB** as the analytical store. Reads nflverse parquet directly; ~500k
  player-weeks across all seasons is trivial for it. No server.
- **Python** (3.14, `uv`-managed venv) for ingestion, features, models, backtests.
- **Node** keeps the Yahoo session, the CLI, and all existing behaviour. It reads
  artifacts the Python side writes; it is not restructured.

Verified installable on this machine: duckdb, pandas, numpy, scikit-learn,
pyarrow, scipy on Python 3.14.7.

## 4. Components

Five packages under `analytics/`, strict dependency order, each independently
testable.

### 4.1 `ingest`
nflverse releases -> local parquet. Weekly player stats (150 cols), schedules,
snap counts, rosters. Idempotent, resumable, one file per season. Join key is
`gsis_id`, which the Sleeper crosswalk already carries.

### 4.2 `features`
Rolling windows over prior weeks only. **Point-in-time correctness is enforced
structurally**, not by convention: every feature function receives an as-of week
and may only read rows strictly before it. This is the leakage guard, and the
single most common way a fantasy model lies to its author.

Feature tiers, per §2:
- **Modelled:** carries, targets, target_share, air_yards_share, wopr, snap share
- **Regressed:** yards per opportunity (shrunk to positional + player prior)
- **Rate x opportunity:** TDs per opportunity, heavily shrunk
- **Small regressed term:** opponent adjustment, QB/WR only

### 4.3 `backtest`
Walk-forward validation with strict temporal splits. Built BEFORE any model.

Baselines a model must beat to justify existing:
1. **ADP order** (already available)
2. **Last-N-week average** (trailing mean)
3. **Season-to-date average**

Metrics: MAE and RMSE on half-PPR points; Spearman rank correlation; and
**calibration** of the distributional output (are 80% intervals right 80% of the
time?). A model that improves MAE while mis-calibrating is worse for lineup
decisions than one that does the reverse.

### 4.4 `models`
```
volume model       -> E[carries], E[targets]      GBM / hierarchical
efficiency prior   -> shrunk yards-per-opportunity  NOT predicted
TD rate            -> shrunk TDs-per-opportunity    heavily regressed
        |
        v
Monte Carlo compose -> distribution of half-PPR points
```
Simulation rather than multiplying means gives the distribution the playoff
optimiser requires, and propagates efficiency's randomness into honest width.

### 4.5 `decide`
| Scenario | Objective | Algorithm |
|---|---|---|
| Draft | max sum of VOR subject to pick timing | VOR vs positional replacement, tiers, ADP survival `P(available at pick k)` |
| Weekly lineup | `argmax sum E[pts]` over legal lineups | Greedy by position with FLEX resolution |
| Playoff | `max P(my total > their total)` | Monte Carlo over both lineups; **variance-seeking when underdog, variance-averse when favoured** |
| Waiver / FAAB | max marginal roster value | VOR(add) - VOR(drop); bid from surplus and budget |
| Defence streaming | deliberately minimal | Justified by §2.2: the signal is ~0 |

The playoff objective is the one most tools get wrong. Maximising expected points
is the wrong objective when you are a large underdog: you need the probability of
exceeding a specific opponent total, which favours ceiling over mean.

## 5. Scoring

League is **half-PPR** (`RECEPTION` modifier 0.5, verified in the committed
league-settings capture). nflverse ships `fantasy_points` (standard) and
`fantasy_points_ppr`; neither is this league's scoring, so points are composed
from components against league settings. The statistically correct architecture
is also the one the league forces.

## 6. Testing

- Every feature function gets a point-in-time test: given an as-of week, it must
  produce identical output whether or not future rows are present in the frame.
  This is the leakage guard's regression test.
- Fixtures are small committed parquet/CSV slices, deterministic and scrubbed.
- The backtest harness is itself tested against a synthetic series with a known
  answer, so a broken harness cannot silently bless a broken model.

## 7. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Leakage inflates offline scores | **high** | model looks good, loses money | structural point-in-time enforcement + tests |
| Overfitting to one season | high | brittle | walk-forward across 2015-2025 |
| nflverse schema drift | medium | ingest breaks | pin asset names; fail loudly |
| Model never beats ADP | medium | wasted effort | baselines are the gate; report honestly |
| Building DvP as if it worked | medium | confident noise | §2.2 caps it at QB/WR, heavily regressed |

## 8. Out of scope

- Live in-game / DFS optimisation.
- Sleeper or ESPN as league providers.
- Any paid data source.
- Automated roster moves. Predictions inform; the human decides.

## 9. Open questions

### 9.1 DvP finding needs multi-season replication
§2.2 rests on 2025 alone. The harness's first job is replicating it across
2015-2025. If the negative correlations do not hold, revisit the opponent term.

### 9.2 Vegas lines not yet sourced
Implied team total is widely the strongest single short-term predictor and is not
in nflverse. No free, reliable, terms-clean source has been identified. Deferred,
and named here so it is not forgotten.

### 9.3 In-season shapes unverified
Everything was probed pre-season (2026-08-31). 2026 has zero games played, so all
modelling uses prior seasons until week 1 completes.
