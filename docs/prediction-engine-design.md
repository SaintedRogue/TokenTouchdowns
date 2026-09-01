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

Single-season measurement, 2025 only:

| Position faced | r |
|---|---|
| QB | 0.167 |
| WR | 0.142 |
| RB | -0.053 |
| TE | -0.088 |

**Eleven-season replication (2015-2025)**, see §9.1 (resolved), shows this
single season does not generalise:

| Position | mean r, 2015-2025 | seasons negative |
|---|---|---|
| QB | 0.103 | 3 / 11 |
| RB | **0.216** | 2 / 11 |
| TE | 0.123 | 4 / 11 |
| WR | 0.105 | 2 / 11 |

RB has the *highest* mean reliability of the four positions across eleven
seasons — the opposite of what the 2025 snapshot suggested. 2025 itself
reproduces (RB -0.030, TE -0.066), but 2025 is an outlier year, not the
general pattern: correlations flip sign across seasons for every position
(QB negative in 3 of 11 years, TE in 4, RB and WR in 2 each).

Points allowed by position largely measures **which offences a team happened to
play**, not defensive quality — and this holds for all four positions, not just
RB/TE. The replication strengthens the architectural conclusion: every
position's mean r sits in roughly the 0.10-0.22 band, well under the ~0.35
threshold for nominal significance at n=32 team-halves, with signs flipping
year to year. No opponent-adjustment subsystem is justified for any position.

The positional split drawn from the single-season finding — adjust QB/WR,
exclude RB/TE — is **removed**. It had no empirical basis beyond one season
and does not survive replication. Any opponent term that is built must be
uniformly small and heavily regressed across all four positions, or omitted
entirely.

### 2.3 Usage metric stability (WR/TE, lag-1)

`target_share` 0.656 · `wopr` 0.646 · `targets` 0.610. Shares edge raw counts and
normalise for team context, so they survive a player changing teams.

## 3. Substrate

- **Phase 1** reads nflverse parquet directly with `pandas.read_parquet`
  (`ingest.load_seasons`); no query engine sits in front of it. At the current
  scale (~500k player-weeks across all seasons) this is sufficient and fast.
  No server.
- **Python** (3.14, `uv`-managed venv) for ingestion, features, models, backtests.
- **Node** keeps the Yahoo session, the CLI, and all existing behaviour. It reads
  artifacts the Python side writes; it is not restructured.

`duckdb` and `scikit-learn` are declared dependencies, verified installable on
this machine alongside pandas, numpy, pyarrow, and scipy (Python 3.14.7), but
unused so far. They are declared ahead of need: Phase 2's query and modelling
layers are where they come in, not Phase 1.

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
- **Small regressed term:** opponent adjustment, uniformly small and heavily
  regressed across all positions if built at all (§2.2)

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

**Measured baseline results** (2024-2025 regular season, QB/RB/WR/TE,
`start_week=5`, half-PPR points):

```
FULL POOL (n=10,241)
  last_3   MAE 4.266   RMSE 6.140   Spearman 0.653
  last_8   MAE 4.080   RMSE 5.827   Spearman 0.673

STARTER-RELEVANT, weeks where the player scored >= 5 (n=4,454)
  last_3   MAE 5.290   RMSE 6.954   Spearman 0.348
  last_8   MAE 5.062   RMSE 6.593   Spearman 0.372
```

`last_8` beats `last_3` on every metric in both pools, so it is the baseline a
model must clear. This measurement covers 2024-2025 only, with no 2023 data
loaded to look back into, so early-2024 folds average `last_8` over fewer than
eight trailing weeks — not a leak or a code defect, but it means the measured
`last_8` bar is very slightly weaker than a full eight-week window on every
fold would give.

The two pools diverge for a specific, measured reason: 25.1% of player-weeks
score under 1 point, and 17.6% score exactly zero. Most of the full pool is
bench players whose near-zero trailing average trivially predicts their
near-zero output — "predict ~0, get ~0" is a free win that pulls MAE down and
inflates Spearman without reflecting any real skill at ordering players who
are actually in play. Restricting to weeks the player scored >= 5 strips that
out, and Spearman roughly halves (0.673 -> 0.372).

Future models are judged on **both** pools. The full-pool numbers are the
literal bar; the starter-relevant Spearman of 0.372 is the meaningful one,
because judging on the full pool alone would understate real progress and
could cause a genuinely good model to be rejected for failing to also nail
bench players nobody would start.

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

Yards are sampled **per opportunity from a Gamma distribution**, not once per
week from a Normal. A single week-level Normal draw makes variance scale with
opportunities squared, so a 25-carry workhorse would come out looking *more*
volatile than a 5-carry backup — backwards, and misleading for the playoff
optimiser — and it clipped roughly 2.3% of draws at zero, an artificial point
mass no real week produces. Summing i.i.d. per-opportunity Gamma draws instead
keeps the mean at the shrunk yards-per-opportunity prior and makes spread
scale with volume rather than volume squared. Verified empirically against the
closed form:

```
CV_total = sqrt((cv^2 + 1) / volume)
```

where `cv` is the per-opportunity coefficient of variation and `volume` is
opportunity count.

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
| Building DvP as if it worked | medium | confident noise | §2.2: uniformly weak across all positions, so any term is heavily regressed or omitted |

## 8. Out of scope

- Live in-game / DFS optimisation.
- Sleeper or ESPN as league providers.
- Any paid data source.
- Automated roster moves. Predictions inform; the human decides.

## 9. Open questions

### 9.1 DvP finding needs multi-season replication — RESOLVED
§2.2 originally rested on 2025 alone, which showed RB and TE negative and
QB/WR weakly positive; that was generalised into a positional rule (adjust
QB/WR, exclude RB/TE). The eleven-season replication (2015-2025) caught the
over-generalisation: RB in fact has the highest mean reliability of the four
positions across those seasons, and every position's correlation flips sign
from year to year, including in years where the sign matched 2025. The 2025
finding was a real measurement, but it was one outlier year mistaken for a
stable positional pattern.

§2.2 has been corrected accordingly: the positional rule is removed, and the
finding that survives is that defence-vs-position is weak and unstable
*everywhere*, all four positions well under the significance threshold at
n=32 — which is a stronger case against building an opponent-adjustment
subsystem than the original single-season read, not a weaker one.

### 9.2 Vegas lines not yet sourced
Implied team total is widely the strongest single short-term predictor and is not
in nflverse. No free, reliable, terms-clean source has been identified. Deferred,
and named here so it is not forgotten.

### 9.3 In-season shapes unverified
Everything was probed pre-season (2026-08-31). 2026 has zero games played, so all
modelling uses prior seasons until week 1 completes.
