"""Season projections built from historical volume, never from raw points.

The project's own measurement across 19,422 real player-weeks (see
docs/prediction-engine-design.md) is the whole reason this module is shaped
the way it is:

    carries 0.825 | targets 0.623 | fantasy points 0.466 | TDs 0.147
    yards per target 0.046 | yards per carry 0.016

Carries, targets and pass attempts are the sticky, predictable part of a
player's usage, so `season_volume` projects them directly, recency-weighted
toward the most recent season. Yards-per-opportunity and touchdown (and
interception) rate are functionally random -- a player's own history there is
mostly noise -- so they are never extrapolated; `project_players` regresses
them hard toward a positional prior via `features.shrunk_rate` and lets that
shrunk rate stand in for "skill". Points are never predicted directly (they
autocorrelate WORSE than any of their own inputs); they are composed from
volume + shrunk rates via `models.compose.simulate_components`, which is also
what turns this into an honest DISTRIBUTION (p10/p50/p90) instead of a single
falsely-precise number.

THREE VOLUME STREAMS, ONE PLAYER (see task-3-report.md for the fuller
writeup): a running back both carries and catches, and a running quarterback
both throws and carries. Each stream has its own efficiency, its own
touchdown rate, and its own scoring (a reception is worth points on its own; a
carry alone is not; an interception is worth NEGATIVE points). This module
simulates the rushing, receiving and passing streams SEPARATELY and sums the
resulting point samples, rather than folding them into one combined
"opportunity" -- the more faithful of the two options the brief allows, at the
cost of one simulation call per stream per player. Every stat column this
module reads (`_with_required_columns`) defaults to zero when a `history`
frame doesn't track it at all, so a frame with no passing columns (e.g. a
skill-position-only fixture) still works: the passing stream just contributes
zero for every player.

`models.compose.simulate_components` returns (opportunities, yards,
touchdowns) rather than a single fused points number, specifically so a
caller can reweight by this league's actual scoring
(`league.scoring_weights`, never the hardcoded HALF_PPR) instead of trusting
`simulate_points`'s fixed 0.1 pt/yard, 6 pt/TD conversion -- and so it can
derive a related count `simulate_components` has no concept of at all
(receptions from targets, interceptions from attempts) from the SAME
per-sample opportunity count that produced the yards and touchdowns for that
stream, preserving the real correlation between a big-opportunity game and
more of everything, rather than an independent, uncorrelated draw.

A shrunk yards-per-opportunity rate can come out negative in real data (a
player with more kneel-downs/tackles-for-loss than positive yardage on a
small sample) -- a negative Gamma scale parameter is meaningless and
`simulate_components` raises rather than silently producing garbage. Domain
knowledge of which shrunk rates are "yards per opportunity" (and therefore
must be clamped at zero) lives here, not in `models/compose.py`, which stays
a generic simulator with no opinion on what its inputs represent.

EXPECTED GAMES ARE PROJECTED, NEVER ASSUMED. A full season (`games=17`) was
previously a constant applied to every player -- which let a career backup
with a single old 5-game stretch and nothing since (a real case this surfaced
on real data) project a FULL season off that one hot stretch's per-game rate.
`season_volume` now projects `proj_games` per player the same way it projects
volume: recency-weighted, then shrunk toward a positional prior via
`features.shrunk_rate`, with the player's own recency-weighted season-count
standing in for "how much evidence do we have". This fixes the whole class of
problem at once (the career backup, the injury-prone starter, the rookie with
no history, the barely-used committee back), not just the one name that
happened to surface it -- a filter on "no recent games" would only have
caught that one case and silently mis-ranked the rest.
"""
from __future__ import annotations

import zlib
from collections.abc import Iterable

import numpy as np
import pandas as pd

from .features import shrunk_rate
from .league import LeagueConfig, scoring_weights
from .models.compose import simulate_components, summarise

# Shrinkage strengths, in units of "opportunities worth of prior weight" (see
# features.shrunk_rate's docstring). These are all quantities the modelling
# principle treats as "functionally random" -- deliberately large relative to
# a typical multi-season workload (a three-year workhorse RB might reach
# ~700-800 carries; a three-year starting QB ~1600-1800 attempts) so that even
# a big sample only partially overrides the positional prior; a single season
# is dominated by the prior outright, which is the intended behaviour, not a
# bug. The ORDERING across the rushing/receiving constants mirrors the
# measured r values directly: yards/carry (r=0.016, the least real signal)
# gets the strongest pull; TD rate (r=0.147, the most real signal of the
# three) gets the weakest. Catch rate and every passing constant aren't among
# the six measured quantities in the spec, so their strengths are reasoned
# middle values, not measured ones -- flagged here rather than presented as
# equally justified.
RUSH_EFF_STRENGTH = 500.0
REC_EFF_STRENGTH = 350.0
RUSH_TD_STRENGTH = 300.0
REC_TD_STRENGTH = 250.0
CATCH_RATE_STRENGTH = 200.0
PASS_EFF_STRENGTH = 1000.0
PASS_TD_STRENGTH = 600.0
PASS_INT_STRENGTH = 600.0

# Expected games played per season: NOT a rate over opportunities like the
# constants above, but the same shrinkage mechanics apply directly if it's
# framed as one -- "games per season", shrunk toward a positional prior, with
# each player's own recency-weighted total season-count standing in for the
# denominator (see season_volume's `_combine`). A single unit here is worth
# one full season of recency weight (season weights run 1, 3, 9, ... under
# the default scheme), so GAMES_STRENGTH=4.0 means roughly "trust a lone
# recent season a bit, but a lone OLD season (weight 1) gets pulled hard
# toward the prior." Kept deliberately small relative to the
# opportunity-count strengths above because the quantity it's shrinking is
# itself small (single digits to 17), not hundreds of carries.
GAMES_STRENGTH = 4.0

# The real NFL regular-season length. proj_games can never legitimately
# exceed this -- it caps both a shrunk estimate that lands above it (rare)
# and any data quirk (extra logged weeks) in the input.
SEASON_LENGTH = 17.0

# Every stat column this module reads. A `history` frame that doesn't track a
# stream at all (e.g. a skill-position-only fixture with no passing columns)
# is filled to zero for the columns it's missing, rather than raising --
# see `_with_required_columns`.
_REQUIRED_STAT_COLUMNS = (
    "carries", "rushing_yards", "rushing_tds",
    "targets", "receiving_yards", "receiving_tds", "receptions",
    "attempts", "passing_yards", "passing_tds", "passing_interceptions",
)
_TOTAL_COLUMNS = _REQUIRED_STAT_COLUMNS

_EMPTY_PRIOR = pd.Series({
    "rush_eff": 0.0, "rush_td_rate": 0.0,
    "rec_eff": 0.0, "rec_td_rate": 0.0, "catch_rate": 0.0,
    "pass_eff": 0.0, "pass_td_rate": 0.0, "pass_int_rate": 0.0,
})


def _with_required_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Every stat column this module reads, defaulting missing ones to zero.

    Lets a frame that doesn't track a whole stream (no passing columns at
    all, say) flow through unchanged rather than KeyError -- that stream
    simply contributes zero for every player, which is correct: zero
    attempts is a true fact about a player this module was never told threw
    a pass, not a missing-data problem to solve.
    """
    df = df.copy()
    for column in _REQUIRED_STAT_COLUMNS:
        if column not in df.columns:
            df[column] = 0.0
    return df


def _default_recency_weights(seasons: Iterable[int]) -> dict[int, float]:
    """Geometric weights, 3x per season of recency.

    The brief asks for "the most recent season roughly 3:1 over the prior
    one"; a constant 3x ratio between EVERY adjacent pair of seasons is the
    natural generalisation to more than two seasons of history, rather than
    inventing a different rule for the 3+ season case.
    """
    ordered = sorted(set(seasons))
    return {season: 3.0 ** i for i, season in enumerate(ordered)}


def season_volume(
    history: pd.DataFrame,
    seasons: Iterable[int],
    recency_weights: dict[int, float] | None = None,
) -> pd.DataFrame:
    """Per-player expected per-game carries, targets, pass attempts, and
    expected 2026 games played (`proj_games`).

    Season-level selection (which seasons feed the projection), not a
    within-season as-of point, so this filters directly rather than routing
    through `features.prior_weeks` -- there is no single "as of week" here,
    the whole point is to look across complete historical seasons to project
    a future one. `studies.baselines.compare_baselines` uses the identical
    `season.isin(...)` pattern for the same reason.

    Each player's per-season rate is computed first, THEN combined across
    seasons with the recency weight -- not the other way around (weighting
    raw weeks) -- so a player who missed half of a good season doesn't have
    that season's rate diluted by its own missed games before the recency
    weight is even applied.

    `proj_games` exists because `games` was previously a caller-supplied
    CONSTANT (assumed 17 for everyone) in `project_players` -- which let a
    career backup with a single old 5-game stretch and nothing since (the
    real case that motivated this) project a full season off that one hot
    stretch's per-game rate. Expected games is itself a prediction: shrunk
    the same way as every other functionally-uncertain quantity in this
    module, toward a positional prior, with the player's own recency-weighted
    season-count standing in as the "how much evidence do we have" term.
    """
    seasons = tuple(seasons)
    if recency_weights is None:
        recency_weights = _default_recency_weights(seasons)

    columns = [
        "player_id", "position",
        "carries_per_game", "targets_per_game", "attempts_per_game",
        "games", "proj_games",
    ]
    history = _with_required_columns(history)
    subset = history[history["season"].isin(seasons)]
    if subset.empty:
        return pd.DataFrame(columns=columns)

    subset = subset.assign(_weight=subset["season"].map(recency_weights).fillna(0.0))

    per_season = (
        subset.groupby(["player_id", "season"])
        .agg(
            position=("position", "last"),
            carries=("carries", "mean"),
            targets=("targets", "mean"),
            attempts=("attempts", "mean"),
            games=("week", "count"),
            weight=("_weight", "first"),
        )
        .reset_index()
    )

    # Positional prior for expected games: an UNWEIGHTED average of games
    # played, across EVERY player-season at the position -- not just
    # full-time starters. This is deliberate: a real position's full stat
    # sheet is dominated by short-stint backups, injury replacements and
    # midseason call-ups, so this average comes out naturally modest. That is
    # what lets a low-evidence player regress toward "we don't expect this
    # person to play much", per the fix brief, rather than toward a
    # flattering league-average-STARTER number a starters-only prior would
    # give.
    games_prior = per_season.groupby("position")["games"].mean()

    def _combine(group: pd.DataFrame) -> pd.Series:
        total_weight = group["weight"].sum()
        position = group.sort_values("season")["position"].iloc[-1]
        if total_weight > 0:
            carries = float((group["carries"] * group["weight"]).sum() / total_weight)
            targets = float((group["targets"] * group["weight"]).sum() / total_weight)
            attempts = float((group["attempts"] * group["weight"]).sum() / total_weight)
            weighted_games = float((group["games"] * group["weight"]).sum())
            games_evidence = float(total_weight)
        else:
            # No season this player appears in carries positive recency
            # weight (e.g. only seasons outside an explicit recency_weights
            # map): a flat mean is the least-wrong fallback, not a
            # ZeroDivisionError or a silently dropped player.
            carries = float(group["carries"].mean())
            targets = float(group["targets"].mean())
            attempts = float(group["attempts"].mean())
            weighted_games = float(group["games"].sum())
            games_evidence = float(len(group))

        prior_games = float(games_prior.get(position, group["games"].mean()))
        proj_games = shrunk_rate(weighted_games, games_evidence, prior_games, GAMES_STRENGTH)
        proj_games = min(max(proj_games, 0.0), SEASON_LENGTH)

        return pd.Series({
            "position": position,
            "carries_per_game": carries,
            "targets_per_game": targets,
            "attempts_per_game": attempts,
            "games": int(group["games"].sum()),
            "proj_games": proj_games,
        })

    out = per_season.groupby("player_id").apply(_combine, include_groups=False).reset_index()
    return out[columns]


def _rate(numerator: pd.Series, denominator: pd.Series) -> float:
    total_num = float(numerator.sum())
    total_den = float(denominator.sum())
    return total_num / total_den if total_den > 0 else 0.0


def _positional_priors(subset: pd.DataFrame) -> pd.DataFrame:
    """League-wide rates per position, from the SAME historical seasons
    project_players is given -- these are what every player's own (heavily
    shrunk) rate regresses toward. Unweighted totals across seasons, unlike
    season_volume's recency weighting: recency matters for volume (which is
    genuinely predictable) but not for rates the spec measures as
    functionally random, so a plain historical average is an equally good,
    simpler estimate of the baseline.
    """
    rows = []
    for position, group in subset.groupby("position"):
        rows.append({
            "position": position,
            "rush_eff": _rate(group["rushing_yards"], group["carries"]),
            "rush_td_rate": _rate(group["rushing_tds"], group["carries"]),
            "rec_eff": _rate(group["receiving_yards"], group["targets"]),
            "rec_td_rate": _rate(group["receiving_tds"], group["targets"]),
            "catch_rate": _rate(group["receptions"], group["targets"]),
            "pass_eff": _rate(group["passing_yards"], group["attempts"]),
            "pass_td_rate": _rate(group["passing_tds"], group["attempts"]),
            "pass_int_rate": _rate(group["passing_interceptions"], group["attempts"]),
        })
    return pd.DataFrame(rows).set_index("position")


def _stable_seed(player_id: str, offset: int) -> int:
    """Deterministic per-player-per-stream seed component.

    Built from crc32, not Python's built-in hash() -- str hashing is
    randomized per process by default (PYTHONHASHSEED), so hash("A") is
    reproducible within one run but not guaranteed across runs, which would
    make a caller-supplied `seed` not actually reproducible end to end.
    """
    return (zlib.crc32(player_id.encode()) % 1_000_000) * 10 + offset


def _resolve_seed(seed: int | None) -> int:
    """A concrete, shareable base seed, even when the caller passes None.

    Every per-player-per-stream seed is derived from this one value (see
    `_stable_seed`), so resolving a concrete integer once here -- rather than
    threading `None` through and letting each stream draw its own
    OS-entropy seed -- keeps "seed=None" meaning "different every call" at
    the project_players level while every stream within one call still uses
    a reproducible, derived seed.
    """
    if seed is not None:
        return int(seed)
    return int(np.random.default_rng().integers(0, 2**31 - 1))


def project_players(
    history: pd.DataFrame,
    config: LeagueConfig,
    seasons: Iterable[int],
    games: int | None = None,
    n: int = 5000,
    seed: int | None = None,
) -> pd.DataFrame:
    """Full season projection per player: volume x shrunk rate, composed.

    Columns: player_id, position, proj_points, p10, p50, p90, sd, proj_games.

    `games` defaults to None, meaning: use each player's own projected games
    (`season_volume`'s `proj_games` -- see its docstring for why this exists
    at all). Passed explicitly, `games` overrides the projection with a flat
    value for every player, exactly as this parameter behaved before expected
    games existed -- callers that want "assume everyone plays N games" (e.g.
    a fixed-games backtest, or the existing games=1/games=17 tests) still get
    exactly that, uncapped, since an explicit value is a deliberate caller
    choice, not a prediction this module needs to defend.
    """
    seasons = tuple(seasons)
    weights = scoring_weights(config)
    history = _with_required_columns(history)
    volume = season_volume(history, seasons)

    subset = history[history["season"].isin(seasons)]
    priors = _positional_priors(subset)
    totals = subset.groupby("player_id")[list(_TOTAL_COLUMNS)].sum().reset_index()

    merged = volume.merge(totals, on="player_id", how="left").fillna(0.0)
    base_seed = _resolve_seed(seed)

    rows = []
    for _, row in merged.iterrows():
        position = row["position"]
        prior = priors.loc[position] if position in priors.index else _EMPTY_PRIOR

        # A shrunk yards-per-opportunity rate becomes a Gamma SCALE parameter
        # inside simulate_components, which cannot be negative. Real data can
        # produce a negative rate on a small, kneel-down/loss-heavy sample --
        # clamped here (not inside compose.py, which has no domain knowledge
        # of which of its float inputs represent yards) because a negative
        # expected yards-per-opportunity isn't a meaningful quantity to carry
        # forward at all, not merely an implementation constraint.
        rush_eff = max(shrunk_rate(row["rushing_yards"], row["carries"], prior["rush_eff"], RUSH_EFF_STRENGTH), 0.0)
        rush_td = shrunk_rate(row["rushing_tds"], row["carries"], prior["rush_td_rate"], RUSH_TD_STRENGTH)
        rec_eff = max(shrunk_rate(row["receiving_yards"], row["targets"], prior["rec_eff"], REC_EFF_STRENGTH), 0.0)
        rec_td = shrunk_rate(row["receiving_tds"], row["targets"], prior["rec_td_rate"], REC_TD_STRENGTH)
        catch_rate = shrunk_rate(row["receptions"], row["targets"], prior["catch_rate"], CATCH_RATE_STRENGTH)
        pass_eff = max(shrunk_rate(row["passing_yards"], row["attempts"], prior["pass_eff"], PASS_EFF_STRENGTH), 0.0)
        pass_td = shrunk_rate(row["passing_tds"], row["attempts"], prior["pass_td_rate"], PASS_TD_STRENGTH)
        pass_int = shrunk_rate(row["passing_interceptions"], row["attempts"], prior["pass_int_rate"], PASS_INT_STRENGTH)

        # `games` explicit overrides the per-player projection for every
        # player (existing games=1/games=17-style callers); otherwise each
        # player uses their OWN season_volume-projected games -- see
        # project_players' and season_volume's docstrings for why a flat
        # constant here was the actual defect behind a 5-career-game backup
        # projecting a full season.
        player_games = float(games) if games is not None else float(row["proj_games"])

        # A single simulate_components call at volume = per_game * player_games
        # is distributionally identical to summing that many independent
        # weekly draws at the per-game volume (Poisson opportunities and,
        # given matching opportunities, Gamma yards and Binomial touchdowns
        # are all closed under summing independent draws at a fixed rate) --
        # so this is the exact season total, not an approximation, without a
        # per-week loop.
        rush_volume = row["carries_per_game"] * player_games
        rec_volume = row["targets_per_game"] * player_games
        pass_volume = row["attempts_per_game"] * player_games

        player_id = row["player_id"]
        rush_seed = base_seed + _stable_seed(player_id, 0)
        rec_seed = base_seed + _stable_seed(player_id, 1)
        catch_seed = base_seed + _stable_seed(player_id, 2)
        pass_seed = base_seed + _stable_seed(player_id, 3)
        int_seed = base_seed + _stable_seed(player_id, 4)

        _, rush_yards, rush_tds = simulate_components(rush_volume, rush_eff, rush_td, n=n, seed=rush_seed)
        rec_opportunities, rec_yards, rec_tds = simulate_components(rec_volume, rec_eff, rec_td, n=n, seed=rec_seed)
        pass_opportunities, pass_yards, pass_tds = simulate_components(pass_volume, pass_eff, pass_td, n=n, seed=pass_seed)

        # Receptions and interceptions: not modelled by simulate_components
        # at all (it only knows yards and touchdowns), yet this league scores
        # both explicitly (positively and negatively respectively). Each is
        # drawn from the opportunity count simulate_components handed back
        # for its own stream -- not reconstructed or re-derived -- so a
        # big-target-share (or big-attempts) sample also gets more receptions
        # (or interceptions), not an uncorrelated count; only the catch/pick
        # outcome itself gets its own independent randomness.
        receptions = np.random.default_rng(catch_seed).binomial(
            rec_opportunities, min(max(catch_rate, 0.0), 1.0)
        )
        interceptions = np.random.default_rng(int_seed).binomial(
            pass_opportunities, min(max(pass_int, 0.0), 1.0)
        )

        points = (
            rush_yards * weights.get("rushing_yards", 0.0)
            + rush_tds * weights.get("rushing_tds", 0.0)
            + rec_yards * weights.get("receiving_yards", 0.0)
            + rec_tds * weights.get("receiving_tds", 0.0)
            + receptions * weights.get("receptions", 0.0)
            + pass_yards * weights.get("passing_yards", 0.0)
            + pass_tds * weights.get("passing_tds", 0.0)
            + interceptions * weights.get("passing_interceptions", 0.0)
        )
        summary = summarise(points)
        rows.append({
            "player_id": player_id,
            "position": position,
            "proj_points": summary["mean"],
            "p10": summary["p10"],
            "p50": summary["p50"],
            "p90": summary["p90"],
            "sd": summary["sd"],
            "proj_games": player_games,
        })

    return pd.DataFrame(
        rows,
        columns=["player_id", "position", "proj_points", "p10", "p50", "p90", "sd", "proj_games"],
    )
