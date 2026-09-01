"""PER-WEEK projections: the distribution a one-week decision actually needs.

WHY THIS MODULE EXISTS. `projections.project_players` returns SEASON totals,
and a season total is a sum of ~17 near-independent weeks, so its relative
spread is smaller than a single week's by roughly sqrt(17) = 4.1x. Measured
on real 2023-2025 data, the season model's coefficient of variation runs
0.482 (0-50 projected points) down to 0.110 (200+); a single NFL week for a
top skill player is nearer 0.5 -- they score 3 points or 30. Season
aggregation averages away exactly the weekly spread a variance-aware decision
depends on, which is why `playoff.playoff_lineup` -- mathematically correct,
per its own module docstring -- returned the SAME lineup as the plain
expected-points optimiser in every real matchup tested. At CV 0.11 no bench
player is ever "equal mean, higher ceiling", so the optimiser has nothing to
trade.

DIVIDING A SEASON `sd` BY 17 DOES NOT FIX THIS, and is the trap this module
exists to avoid. For a sum of ~independent weeks the correct scaling is
sqrt(17), so naive division understates weekly spread by ~4x -- landing on a
number even tighter than the season figure it was trying to widen.
`test_weekly_sd_is_the_sqrt_of_seventeen_scaling_not_a_naive_divide_by_
seventeen` pins the magnitude, not the direction.

THE MODEL IS NOT A NEW MODEL. `models.compose.simulate_components` is
VOLUME-PARAMETERISED: its own closed form is CV_total = sqrt((cv^2+1)/volume),
so spread falls out of the volume it is handed. `projections.project_players`
feeds it `per_game_volume * proj_games` and gets a season; this module feeds
it `per_game_volume * 1` and gets a week, using the identical shrunk rates,
the identical three streams, and the identical `league.scoring_weights`
summation. That equivalence is not assumed, it is PINNED: with volume
over-dispersion switched off (`volume_dispersion=1.0`), `project_week`
reproduces `project_players(games=1)` EXACTLY, seed for seed (see
`test_poisson_mode_reproduces_project_players_at_one_game_exactly`). The loop
below is duplicated from `projections.py` rather than shared only because
that module's composition is inlined in `project_players` and this branch
must not modify it; every constant, prior and helper it uses is IMPORTED from
there, so a shrinkage constant can never drift between the two.

    Consequence worth stating plainly, because it is the mean-calibration
    result: exp_points == proj_points / proj_games, to Monte Carlo error.
    Measured on 871 real players, ratio mean 1.0006, median 0.9995, with the
    1st and 99th percentiles at 0.979 and 1.027.

VOLUME OVER-DISPERSION: MEASURED, NOT FITTED. `simulate_components` draws
opportunities as Poisson, whose variance equals its mean. Real weekly usage
is more volatile than that -- a starter's workload swings with game script,
committee splits and mid-season role changes. Measured on 2022-2024 nflverse
data as a pooled within-player variance-to-mean ratio (`volume_dispersion`):

    carries   QB 1.35  RB 2.55  TE 1.73  WR 1.14
    targets   QB 0.99  RB 1.37  TE 1.22  WR 1.32
    attempts  QB 2.82  RB 1.11  TE 1.07  WR 1.02

Poisson would be 1.00 everywhere. Ignoring this leaves the model
UNDER-dispersed against observed 2025 weekly scoring by 8-21% (sd ratios
QB 0.798, RB 0.793, TE 0.919, WR 0.901; 0.861 overall); modelling it lands
at QB 0.948, RB 0.975, TE 0.969, WR 0.972 (0.968 overall) while leaving
every mean unchanged (1.016 overall). That is a measured
input, not a fudge factor tuned to the answer: the numbers above come from
the volume columns themselves, are estimated per position from whatever
`history` the caller passes, and the calibration test that grades them never
touches them. `volume_dispersion=1.0` recovers the pure-Poisson model for
anyone who wants to see the residual for themselves.

    HOW IT REUSES THE COMPOSER RATHER THAN REPLACING IT. A Poisson whose
    rate is itself Gamma-distributed is a negative binomial: with
    lambda ~ Gamma(k, d-1) and k = volume/(d-1), the mixture has mean
    `volume` and variance `volume * d` exactly. So over-dispersed volume is
    simply `simulate_components` called at a SPREAD of volumes instead of
    one. `_STRATA` equal-probability CONDITIONAL MEANS of that Gamma are
    used (deterministic, so the mixture itself adds no Monte Carlo noise);
    they average to the projected volume exactly by construction, which is
    why over-dispersion moves `sd` and never `exp_points`. A finite set of
    points cannot carry the full Gamma variance, so the realised
    over-dispersion is 3-7% short of the measured target -- documented in
    `_gamma_strata`, and a bias toward UNDER-dispersion, never toward
    flattering the calibration. `compose.py` is not modified, and is not
    asked to know anything about football.

CONDITIONAL OR MARGINAL: BOTH, NAMED. Over a season a missed game reduces
volume. In a single week a player who does not play scores EXACTLY ZERO --
a large, discrete, genuinely bimodal variance source with no analogue in the
season model, and precisely what a variance-aware playoff decision should
care about. Two different numbers are defensible here and a caller who
guesses wrong is badly misled, so this module ships both and names them:

  - `exp_points`, `sd`, `p10`, `p50`, `p90` are CONDITIONAL ON PLAYING.
    This is the default and the normal lineup case: you are choosing between
    players you have already decided are active. It is also the only view
    that satisfies exp_points == proj_points / proj_games.
  - `exp_points_marginal`, `sd_marginal`, `p10_marginal`, `p50_marginal`,
    `p90_marginal` are MARGINAL OVER `p_active` -- the conditional
    distribution mixed with a point mass at zero. This is the view for a
    genuinely uncertain injury. Var(marginal) = p*sd^2 + p*(1-p)*mean^2, so
    for a coin-flip player the p*(1-p)*mean^2 term DOMINATES; scaling the
    conditional sd by p (a natural-looking mistake) understates it badly.
  - `p_active` is exposed on every row so a caller can mix its own.

  `for_lineup` maps whichever view the caller picks onto the `proj_points` /
  `sd` column names `lineup.optimal_lineup` and `playoff.playoff_lineup`
  read, so the choice is made explicitly at the call site rather than
  inferred from a column name.

WHAT `p_active` IS AND IS NOT. It is `proj_games / SEASON_LENGTH` -- the
season model's own projected availability, which is already shrunk toward a
positional prior (see `projections.GAMES_STRENGTH`). Validated against 2025:
for players still active in the league, predicted vs observed appearance
rates were 0.36/0.27, 0.51/0.46, 0.71/0.73, 0.81/0.72 by bucket -- good.
Across ALL 2022-2024 players it badly over-predicts (0.52 predicted vs 0.17
observed in the middle bucket) because roughly half of the low-`proj_games`
population simply never appears again: retired, cut, out of the league. THIS
MODULE DOES NOT MODEL ROSTER ATTRITION. `p_active` answers "given this player
is on an NFL roster, does he suit up this week", which is the right question
for a lineup decision (he is on YOUR roster) and the wrong question for a
draft-board availability estimate. It also cannot know BYE WEEKS: a bye is a
certain zero that no historical rate can predict, and the caller who knows
the schedule must handle it.

INHERITED SIMPLIFICATIONS, stated rather than hidden. The three streams are
simulated with independent seeds, so a player's rushing and receiving weeks
are uncorrelated here exactly as they are in the season model -- real game
script correlates them. And, as `playoff.py`'s own docstring says, teammates
are independent downstream too. Both understate the spread of a stacked
lineup; neither is introduced by this module.
"""
from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd
from scipy import stats as _stats

from .features import prior_weeks
from .league import LeagueConfig, scoring_weights
from .models.compose import simulate_components, summarise
from .projections import (
    CATCH_RATE_STRENGTH,
    PASS_EFF_STRENGTH,
    PASS_INT_STRENGTH,
    PASS_TD_STRENGTH,
    PROJECTABLE_POSITIONS,
    REC_EFF_STRENGTH,
    REC_FUMBLE_STRENGTH,
    REC_TD_STRENGTH,
    RUSH_EFF_STRENGTH,
    RUSH_FUMBLE_STRENGTH,
    RUSH_TD_STRENGTH,
    SACK_FUMBLE_STRENGTH,
    SEASON_LENGTH,
    _EMPTY_PRIOR,
    _REQUIRED_STAT_COLUMNS,
    _positional_priors,
    _resolve_names,
    _resolve_seed,
    _stable_seed,
    _validate_scoring_is_simulated,
    _warn_about_unmodellable_scoring,
    _with_required_columns,
    regular_season,
    season_volume,
    shrunk_rate,
)

# One "game" of volume. Named rather than written as a bare 1.0 at the three
# call sites, because it is the ENTIRE difference between this module and
# `projections.project_players` (which uses `proj_games` there instead) and
# should be impossible to mistake for an unrelated literal.
ONE_GAME = 1.0

# Strata used to turn a scalar volume into a Poisson-Gamma (negative
# binomial) volume without modifying `compose.simulate_components` -- see the
# module docstring and `_gamma_strata`. Twenty-four is a measured
# cost/accuracy compromise, not a round number: the mean is exact at ANY
# count (conditional means, see `_gamma_strata`), while the realised variance
# converges slowly, reaching 0.93-0.98 of target at 24 and needing ~128 to
# pass 0.99 -- an 5x runtime for a residual smaller than the model's own
# calibration error. Deterministic strata rather than random Gamma draws, so
# the mixture contributes no Monte Carlo noise of its own on top of the
# composer's.
_STRATA = 24

# Minimum weeks a player-season must contain before it contributes to a
# `volume_dispersion` estimate. A two-week sample's variance-to-mean ratio is
# almost pure noise; six weeks is enough for the pooled estimator to be
# stable while still admitting most of a real position's population. The
# estimate is POOLED across player-seasons (see `volume_dispersion`), so this
# is a noise filter on the inputs, not a starters-only restriction of the
# kind `season_volume`'s games prior deliberately avoids.
MIN_DISPERSION_GAMES = 6

# The three volume streams, in the order `project_week` simulates them, each
# paired with the `season_volume` column carrying its per-game projection.
_VOLUME_STREAMS: tuple[tuple[str, str], ...] = (
    ("carries", "carries_per_game"),
    ("targets", "targets_per_game"),
    ("attempts", "attempts_per_game"),
)

# Per-player-per-stream seed offsets. IDENTICAL to the offsets
# `projections.project_players` uses, which is what makes
# `volume_dispersion=1.0` reproduce it bit for bit rather than merely
# approximately -- see the module docstring. Offset 8 (the availability
# Bernoulli) is this module's own addition and deliberately sits past the
# season model's last one.
_RUSH_SEED, _REC_SEED, _CATCH_SEED, _PASS_SEED = 0, 1, 2, 3
_INT_SEED, _RUSH_FUMBLE_SEED, _REC_FUMBLE_SEED, _SACK_FUMBLE_SEED = 4, 5, 6, 7
_ACTIVE_SEED = 8

_OUTPUT_COLUMNS = [
    "player_id", "name", "position",
    "exp_points", "sd", "p10", "p50", "p90",
    "p_active",
    "exp_points_marginal", "sd_marginal",
    "p10_marginal", "p50_marginal", "p90_marginal",
    "proj_games",
]


def volume_dispersion(
    history: pd.DataFrame,
    column: str,
    min_games: int = MIN_DISPERSION_GAMES,
) -> pd.Series:
    """Per-position variance-to-mean ratio of WEEKLY `column`, pooled across
    player-seasons. Poisson is 1.0; anything above is over-dispersion the
    composer's Poisson opportunity draw does not capture on its own.

    WITHIN a player-season, not across players. Across players the ratio
    would mostly measure the (very large, entirely predictable) difference
    between a workhorse and a backup, which `season_volume` already projects
    per player; what is wanted here is how much ONE player's usage swings
    week to week around his OWN average, which is the part the simulator has
    to generate.

    Pooled as `sum((n_i - 1) * var_i) / sum((n_i - 1) * mean_i)` rather than
    as a mean of per-player ratios: a per-player ratio is a ratio of two
    noisy small-sample quantities and its average is dominated by low-volume
    players whose individual estimates are near-meaningless. The pooled form
    weights each player-season by its own evidence and is what makes the
    estimate stable enough to use as a model input (measured on real data,
    the pooled and median-of-ratios estimates agree to ~10%).

    FLOORED AT 1.0. Real usage can be UNDER-dispersed (a perfectly steady
    workload has variance zero), and a Poisson-Gamma mixture cannot represent
    that at all -- its variance is bounded BELOW by the Poisson case. Flooring
    here means an under-dispersed stream falls back to plain Poisson, which is
    the closest representable model, instead of silently producing an invalid
    negative Gamma shape downstream.
    """
    if history.empty or column not in history.columns:
        return pd.Series(dtype=float)

    grouped = history.groupby(["player_id", "season"]).agg(
        position=("position", "last"),
        weeks=(column, "size"),
        mean=(column, "mean"),
        variance=(column, "var"),
    )
    grouped = grouped[
        (grouped["weeks"] >= min_games) & (grouped["mean"] > 0) & grouped["variance"].notna()
    ]
    if grouped.empty:
        return pd.Series(dtype=float)

    evidence = grouped["weeks"] - 1
    grouped = grouped.assign(
        _numerator=evidence * grouped["variance"],
        _denominator=evidence * grouped["mean"],
    )
    totals = grouped.groupby("position")[["_numerator", "_denominator"]].sum()
    ratio = totals["_numerator"] / totals["_denominator"].where(totals["_denominator"] > 0)
    return ratio.fillna(1.0).clip(lower=1.0)


def stream_dispersion(
    history: pd.DataFrame, min_games: int = MIN_DISPERSION_GAMES
) -> dict[str, pd.Series]:
    """`volume_dispersion` for each of the three volume streams at once,
    keyed by the nflverse column name the stream is projected from.
    """
    return {
        column: volume_dispersion(history, column, min_games=min_games)
        for column, _ in _VOLUME_STREAMS
    }


def _gamma_strata(volume: float, dispersion: float, strata: int) -> np.ndarray:
    """The `strata` equal-probability CONDITIONAL MEANS of the Gamma mixing
    distribution that turns Poisson(volume) into a negative binomial with
    variance `volume * dispersion` -- see the module docstring.

    Returns a single-element array (the volume itself) whenever the mixture
    would be degenerate: no volume to spread, or dispersion at/below the
    Poisson floor. That path is what `volume_dispersion=1.0` takes, and it
    hands `simulate_components` exactly what `project_players` would.

    CONDITIONAL MEANS, NOT QUANTILE MIDPOINTS, and the difference is not
    cosmetic. For Gamma(a, s) the identity `int x f_a(x) dx = a*s*F_{a+1}(x)`
    gives each stratum's exact conditional mean in closed form, and equal-
    probability strata therefore average to the distribution mean EXACTLY --
    so `exp_points` is untouched by dispersion as a matter of construction,
    with no rescaling step to get wrong. Quantile midpoints do not: on a
    skewed Gamma they average BELOW the mean by up to 5% at low volume and
    high dispersion (measured: 0.949 of the true mean at volume 1,
    dispersion 3), which would silently turn a spread parameter into a
    location one for exactly the low-volume players a lineup decision is
    most likely to be marginal on.

    WHAT THIS DISCRETISATION STILL LOSES, stated rather than hidden: any
    finite set of points captures only the BETWEEN-stratum variance and
    drops the within-stratum part, so the realised variance is short of
    `volume * (dispersion - 1)`. Measured at `_STRATA` = 24: 0.93 of target
    at volume 1 / dispersion 3, 0.97 at volume 3 / dispersion 2.5, 0.98 at
    volume 18 / dispersion 2.5. The model is therefore very slightly LESS
    over-dispersed than the measured input asks for -- which biases the
    calibration below toward under-dispersion, never toward flattering it.
    `test_gamma_strata_deliver_most_of_the_requested_variance` pins that
    band so the shortfall cannot quietly grow.
    """
    if volume <= 0.0 or dispersion <= 1.0 or strata < 2:
        return np.array([max(volume, 0.0)], dtype=float)
    scale = dispersion - 1.0
    shape = volume / scale
    edges = _stats.gamma.ppf(np.linspace(0.0, 1.0, strata + 1), a=shape, scale=scale)
    edges[0], edges[-1] = 0.0, np.inf
    upper = _stats.gamma.cdf(edges[1:], a=shape + 1.0, scale=scale)
    lower = _stats.gamma.cdf(edges[:-1], a=shape + 1.0, scale=scale)
    points = shape * scale * (upper - lower) * strata
    if not np.all(np.isfinite(points)) or points.min() < 0.0:
        return np.array([volume], dtype=float)
    return points


def _strata_sizes(n: int, strata: int) -> np.ndarray:
    """How many of the `n` samples each stratum draws, summing to exactly `n`."""
    edges = np.round(np.linspace(0, n, strata + 1)).astype(int)
    return np.diff(edges)


def _simulate_stream(
    volume: float, eff_rate: float, td_rate: float, n: int, seed: int, dispersion: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """`simulate_components` for one stream at one week's volume, with the
    volume itself over-dispersed by `dispersion` (1.0 = plain Poisson).

    The single-stratum path calls `simulate_components(volume, ..., seed=seed)`
    with the caller's seed UNCHANGED, which is what makes
    `volume_dispersion=1.0` reproduce `projections.project_players(games=1)`
    exactly rather than merely closely. The multi-stratum path derives each
    stratum's seed from that same seed via `SeedSequence`, so the result stays
    reproducible without any stratum reusing another player's stream.
    """
    strata = min(_STRATA, max(n, 1))
    points = _gamma_strata(volume, dispersion, strata)
    if len(points) == 1:
        return simulate_components(float(points[0]), eff_rate, td_rate, n=n, seed=seed)
    sizes = _strata_sizes(n, len(points))

    stratum_seeds = np.random.SeedSequence(int(seed)).generate_state(len(points))
    opportunities, yards, touchdowns = [], [], []
    for point, size, stratum_seed in zip(points, sizes, stratum_seeds, strict=True):
        if size <= 0:
            continue
        one, two, three = simulate_components(
            float(point), eff_rate, td_rate, n=int(size), seed=int(stratum_seed)
        )
        opportunities.append(one)
        yards.append(two)
        touchdowns.append(three)
    return (
        np.concatenate(opportunities),
        np.concatenate(yards),
        np.concatenate(touchdowns),
    )


def _binomial(opportunities: np.ndarray, rate: float, seed: int) -> np.ndarray:
    """A count derived from the SAME opportunity draw that produced this
    stream's yards and touchdowns -- receptions from targets, interceptions
    and sack fumbles from attempts -- so a big-volume week also gets more of
    everything, rather than an uncorrelated draw. Identical to
    `projections.project_players`' own handling.
    """
    return np.random.default_rng(seed).binomial(opportunities, min(max(rate, 0.0), 1.0))


def _dispersion_for(
    measured: dict[str, pd.Series], column: str, position: str, override: float | None
) -> float:
    if override is not None:
        return max(float(override), 1.0)
    series = measured.get(column)
    if series is None or position not in series.index:
        return 1.0
    return float(series.loc[position])


def project_week(
    history: pd.DataFrame,
    config: LeagueConfig,
    seasons: Iterable[int],
    n: int = 5000,
    seed: int | None = None,
    as_of_season: int | None = None,
    as_of_week: int | None = None,
    volume_dispersion: float | None = None,
) -> pd.DataFrame:
    """Per-player PER-WEEK points distribution -- the season model's pipeline
    run at one game's volume, plus availability.

    Columns (see the module docstring for the conditional/marginal split,
    which is the one thing a caller must not guess at):

      player_id, name, position
      exp_points, sd, p10, p50, p90     CONDITIONAL on the player being active
      p_active                          P(on the field in a given team game)
      exp_points_marginal, sd_marginal,
      p10_marginal, p50_marginal,
      p90_marginal                      MARGINAL over p_active (zero if out)
      proj_games                        what p_active was derived from

    `seasons` selects which historical seasons feed the projection, exactly
    as in `projections.project_players`.

    `as_of_season` / `as_of_week`, when both given, additionally cut `history`
    at that point through `features.prior_weeks` -- THE leakage guard, which
    raises `PointInTimeError` if its own postcondition is ever violated. This
    is what makes an in-season weekly projection honest: week 9's projection
    sees weeks 1-8 of the same season and nothing at or after week 9. Omitted
    (the default), the projection is a pre-season one and `seasons` alone
    decides what it may read -- the same discipline `season_volume` applies,
    for the same reason (there is no single as-of point when projecting from
    complete seasons).

    `volume_dispersion` defaults to None, meaning: MEASURE it per position
    per stream from `history` itself (`stream_dispersion`), the same way
    positional efficiency priors are measured rather than assumed. Pass a
    float to override every position and stream with one value; `1.0`
    recovers the plain-Poisson composer and therefore
    `project_players(games=1)` exactly. Values below 1.0 are clamped up to
    it, since a Poisson-Gamma mixture cannot be under-dispersed.
    """
    seasons = tuple(seasons)
    weights = scoring_weights(config)
    _validate_scoring_is_simulated(weights)
    _warn_about_unmodellable_scoring(config)

    history = regular_season(history)
    history = _with_required_columns(history)
    history = history[history["position"].isin(PROJECTABLE_POSITIONS)]
    if as_of_season is not None and as_of_week is not None:
        history = prior_weeks(history, as_of_season, as_of_week)

    names = _resolve_names(history)
    volume = season_volume(history, seasons)

    subset = history[history["season"].isin(seasons)]
    priors = _positional_priors(subset)
    measured_dispersion = (
        stream_dispersion(subset) if volume_dispersion is None else {}
    )
    totals = subset.groupby("player_id")[list(_REQUIRED_STAT_COLUMNS)].sum().reset_index()

    merged = volume.merge(totals, on="player_id", how="left").fillna(0.0)
    merged["name"] = merged["player_id"].map(names)
    base_seed = _resolve_seed(seed)

    rows = []
    for _, row in merged.iterrows():
        position = row["position"]
        prior = priors.loc[position] if position in priors.index else _EMPTY_PRIOR
        player_id = row["player_id"]

        # Identical shrinkage to projections.project_players, using the SAME
        # imported constants -- see this module's docstring for why the loop
        # is duplicated and how that duplication is pinned.
        rush_eff = max(shrunk_rate(row["rushing_yards"], row["carries"], prior["rush_eff"], RUSH_EFF_STRENGTH), 0.0)
        rush_td = shrunk_rate(row["rushing_tds"], row["carries"], prior["rush_td_rate"], RUSH_TD_STRENGTH)
        rush_fumble = shrunk_rate(row["rushing_fumbles_lost"], row["carries"], prior["rush_fumble_rate"], RUSH_FUMBLE_STRENGTH)
        rec_eff = max(shrunk_rate(row["receiving_yards"], row["targets"], prior["rec_eff"], REC_EFF_STRENGTH), 0.0)
        rec_td = shrunk_rate(row["receiving_tds"], row["targets"], prior["rec_td_rate"], REC_TD_STRENGTH)
        catch_rate = shrunk_rate(row["receptions"], row["targets"], prior["catch_rate"], CATCH_RATE_STRENGTH)
        rec_fumble = shrunk_rate(row["receiving_fumbles_lost"], row["targets"], prior["rec_fumble_rate"], REC_FUMBLE_STRENGTH)
        pass_eff = max(shrunk_rate(row["passing_yards"], row["attempts"], prior["pass_eff"], PASS_EFF_STRENGTH), 0.0)
        pass_td = shrunk_rate(row["passing_tds"], row["attempts"], prior["pass_td_rate"], PASS_TD_STRENGTH)
        pass_int = shrunk_rate(row["passing_interceptions"], row["attempts"], prior["pass_int_rate"], PASS_INT_STRENGTH)
        sack_fumble = shrunk_rate(row["sack_fumbles_lost"], row["attempts"], prior["sack_fumble_rate"], SACK_FUMBLE_STRENGTH)

        # ONE GAME of volume, where project_players uses proj_games. That
        # single substitution is the whole model change -- the composer is
        # volume-parameterised, so per-game volume yields per-game spread.
        rush_volume = row["carries_per_game"] * ONE_GAME
        rec_volume = row["targets_per_game"] * ONE_GAME
        pass_volume = row["attempts_per_game"] * ONE_GAME

        rush_dispersion = _dispersion_for(measured_dispersion, "carries", position, volume_dispersion)
        rec_dispersion = _dispersion_for(measured_dispersion, "targets", position, volume_dispersion)
        pass_dispersion = _dispersion_for(measured_dispersion, "attempts", position, volume_dispersion)

        rush_opportunities, rush_yards, rush_tds = _simulate_stream(
            rush_volume, rush_eff, rush_td, n, base_seed + _stable_seed(player_id, _RUSH_SEED), rush_dispersion
        )
        rec_opportunities, rec_yards, rec_tds = _simulate_stream(
            rec_volume, rec_eff, rec_td, n, base_seed + _stable_seed(player_id, _REC_SEED), rec_dispersion
        )
        pass_opportunities, pass_yards, pass_tds = _simulate_stream(
            pass_volume, pass_eff, pass_td, n, base_seed + _stable_seed(player_id, _PASS_SEED), pass_dispersion
        )

        components = {
            "rushing_yards": rush_yards,
            "rushing_tds": rush_tds,
            "rushing_fumbles_lost": _binomial(rush_opportunities, rush_fumble, base_seed + _stable_seed(player_id, _RUSH_FUMBLE_SEED)),
            "receiving_yards": rec_yards,
            "receiving_tds": rec_tds,
            "receptions": _binomial(rec_opportunities, catch_rate, base_seed + _stable_seed(player_id, _CATCH_SEED)),
            "receiving_fumbles_lost": _binomial(rec_opportunities, rec_fumble, base_seed + _stable_seed(player_id, _REC_FUMBLE_SEED)),
            "passing_yards": pass_yards,
            "passing_tds": pass_tds,
            "passing_interceptions": _binomial(pass_opportunities, pass_int, base_seed + _stable_seed(player_id, _INT_SEED)),
            "sack_fumbles_lost": _binomial(pass_opportunities, sack_fumble, base_seed + _stable_seed(player_id, _SACK_FUMBLE_SEED)),
        }

        points = np.zeros(len(rush_yards))
        for stat, weight in weights.items():
            points = points + components[stat] * weight

        proj_games = float(row["proj_games"])
        p_active = min(max(proj_games / SEASON_LENGTH, 0.0), 1.0)
        # The marginal view is built by MASKING the conditional samples, not
        # by an analytic formula: a Bernoulli(p_active) draw zeroes the weeks
        # the player sits. That is what puts a real point mass at zero into
        # the marginal percentiles -- p10_marginal is exactly 0.0 for a
        # coin-flip player, which no rescaling of a conditional p10 produces.
        active = np.random.default_rng(
            base_seed + _stable_seed(player_id, _ACTIVE_SEED)
        ).random(len(points)) < p_active
        marginal = np.where(active, points, 0.0)

        conditional_summary = summarise(points)
        marginal_summary = summarise(marginal)
        rows.append({
            "player_id": player_id,
            "name": row["name"],
            "position": position,
            "exp_points": conditional_summary["mean"],
            "sd": conditional_summary["sd"],
            "p10": conditional_summary["p10"],
            "p50": conditional_summary["p50"],
            "p90": conditional_summary["p90"],
            "p_active": p_active,
            "exp_points_marginal": marginal_summary["mean"],
            "sd_marginal": marginal_summary["sd"],
            "p10_marginal": marginal_summary["p10"],
            "p50_marginal": marginal_summary["p50"],
            "p90_marginal": marginal_summary["p90"],
            "proj_games": proj_games,
        })

    return pd.DataFrame(rows, columns=_OUTPUT_COLUMNS)


def for_lineup(weekly: pd.DataFrame, marginal: bool = False) -> pd.DataFrame:
    """`project_week`'s output relabelled for `lineup.optimal_lineup` /
    `playoff.playoff_lineup`, which read `proj_points` and `sd`.

    `marginal=False` (the default) hands over the CONDITIONAL numbers: the
    right choice for the normal case where the players under consideration
    are known to be active. `marginal=True` hands over the availability-
    weighted ones, for a roster with genuine injury doubt.

    This exists so that choice is made explicitly, in one visible argument,
    rather than by a caller reading `exp_points` off a frame that also
    carries `exp_points_marginal` and hoping it picked the one it meant --
    the two differ by a factor of two for a coin-flip player, and the
    optimiser downstream has no way to tell which it was given.
    """
    prefix = "exp_points_marginal" if marginal else "exp_points"
    sd_column = "sd_marginal" if marginal else "sd"
    out = weekly.copy()
    out["proj_points"] = out[prefix]
    out["sd"] = out[sd_column]
    return out
