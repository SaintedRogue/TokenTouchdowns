"""Season projections built from historical volume, never from raw points.

The project's own measurement across 19,422 real player-weeks (see
docs/prediction-engine-design.md) is the whole reason this module is shaped
the way it is:

    carries 0.825 | targets 0.623 | fantasy points 0.466 | TDs 0.147
    yards per target 0.046 | yards per carry 0.016

Carries and targets are the sticky, predictable part of a player's usage, so
`season_volume` projects them directly, recency-weighted toward the most
recent season. Yards-per-opportunity and touchdown rate are functionally
random -- a player's own history there is mostly noise -- so they are never
extrapolated; `project_players` regresses them hard toward a positional prior
via `features.shrunk_rate` and lets that shrunk rate stand in for "skill".
Points are never predicted directly (they autocorrelate WORSE than any of
their own inputs); they are composed from volume + shrunk rates via
`models.compose.simulate_points`, which is also what turns this into an
honest DISTRIBUTION (p10/p50/p90) instead of a single falsely-precise number.

TWO VOLUME STREAMS, ONE PLAYER (see task-3-report.md for the fuller writeup):
a running back both carries and catches, and those two streams have
different efficiencies, different touchdown rates, AND different scoring
(a reception is worth points on its own; a carry is not). This module
simulates the rushing and receiving streams SEPARATELY and sums the
resulting point samples, rather than folding carries+targets into one
combined "opportunity" -- the more faithful of the two options the brief
allows, at the cost of twice the simulation work per player.

`models.compose.simulate_points` bakes in a fixed conversion (0.1 pt/yard,
6 pt/TD) and has no notion of receptions at all, so this league's actual
scoring (`league.scoring_weights`, never the hardcoded HALF_PPR) has to be
applied on top rather than trusted to be baked into that function's return
value. `_extract_yards_and_tds` recovers the underlying yards and touchdown
samples from two calls to `simulate_points` that share a seed (see its
docstring for why that is exact, not a hack), so this module can reweight
them by the league's real per-yard and per-touchdown values and add a
reception term `simulate_points` was never built to know about.
"""
from __future__ import annotations

import zlib
from collections.abc import Iterable

import numpy as np
import pandas as pd

from .features import shrunk_rate
from .league import LeagueConfig, scoring_weights
from .models.compose import TD_POINTS, YARDS_POINT, simulate_points, summarise

# Shrinkage strengths, in units of "opportunities worth of prior weight" (see
# features.shrunk_rate's docstring). All three of these are the measured
# near-zero-autocorrelation quantities the modelling principle calls
# "functionally random" -- deliberately large relative to a typical
# multi-season workload (a three-year workhorse RB might reach ~700-800
# carries) so that even a big sample only partially overrides the positional
# prior; a single season is dominated by the prior outright, which is the
# intended behaviour, not a bug. The ORDERING across constants mirrors the
# measured r values directly: yards/carry (r=0.016, the least real signal)
# gets the strongest pull; TD rate (r=0.147, the most real signal of the
# three) gets the weakest. Catch rate isn't one of the six measured
# quantities in the spec, so its strength is a reasoned middle value, not a
# measured one -- flagged here rather than presented as equally justified.
RUSH_EFF_STRENGTH = 500.0
REC_EFF_STRENGTH = 350.0
RUSH_TD_STRENGTH = 300.0
REC_TD_STRENGTH = 250.0
CATCH_RATE_STRENGTH = 200.0

_TOTAL_COLUMNS = (
    "carries", "rushing_yards", "rushing_tds",
    "targets", "receiving_yards", "receiving_tds", "receptions",
)


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
    """Per-player expected per-game carries and targets.

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
    """
    seasons = tuple(seasons)
    if recency_weights is None:
        recency_weights = _default_recency_weights(seasons)

    columns = ["player_id", "position", "carries_per_game", "targets_per_game", "games"]
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
            games=("week", "count"),
            weight=("_weight", "first"),
        )
        .reset_index()
    )

    def _combine(group: pd.DataFrame) -> pd.Series:
        total_weight = group["weight"].sum()
        if total_weight > 0:
            carries = float((group["carries"] * group["weight"]).sum() / total_weight)
            targets = float((group["targets"] * group["weight"]).sum() / total_weight)
        else:
            # No season this player appears in carries positive recency
            # weight (e.g. only seasons outside an explicit recency_weights
            # map): a flat mean is the least-wrong fallback, not a
            # ZeroDivisionError or a silently dropped player.
            carries = float(group["carries"].mean())
            targets = float(group["targets"].mean())
        return pd.Series({
            "position": group.sort_values("season")["position"].iloc[-1],
            "carries_per_game": carries,
            "targets_per_game": targets,
            "games": int(group["games"].sum()),
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

    `_extract_yards_and_tds` decomposes one stream into two `simulate_points`
    calls that MUST share a seed (that is what makes the recovered yards and
    touchdown samples correlated through a common opportunity count -- see
    its docstring). Passing seed=None straight through would give each of
    those two calls its own independent OS-entropy seed and silently
    decorrelate yards from touchdowns. Resolving a concrete seed once here
    keeps "seed=None" meaning "different every call" at the project_players
    level while still sharing correctly within a call.
    """
    if seed is not None:
        return int(seed)
    return int(np.random.default_rng().integers(0, 2**31 - 1))


def _extract_yards_and_tds(
    volume: float, eff_rate: float, td_rate: float, n: int, seed: int, yards_cv: float = 0.9,
) -> tuple[np.ndarray, np.ndarray]:
    """Recover separate yards and touchdown-count samples from simulate_points.

    simulate_points bakes in fixed weights (YARDS_POINT=0.1, TD_POINTS=6.0)
    and returns only their SUM, so there is nothing in its return value to
    reweight by this league's actual scoring on its own. Calling it twice
    with the SAME seed -- once with td_rate=0 (isolates yards*YARDS_POINT)
    and once with eff_rate=0 (isolates tds*TD_POINTS) -- recovers each
    component exactly, dividing out the known constants.

    This is exact, not approximate: the very first random draw inside
    simulate_points is `rng.poisson(volume, size=n)` from a freshly-seeded
    generator, so with matching seed/volume/n it is bit-identical between
    the two calls. That shared opportunity count is what preserves the real
    correlation between a big-target-share game and BOTH more yards and more
    touchdowns, instead of treating the two as independent draws.
    """
    yards_points = simulate_points(volume, eff_rate, 0.0, n=n, seed=seed, yards_cv=yards_cv)
    td_points = simulate_points(volume, 0.0, td_rate, n=n, seed=seed, yards_cv=yards_cv)
    return yards_points / YARDS_POINT, td_points / TD_POINTS


def project_players(
    history: pd.DataFrame,
    config: LeagueConfig,
    seasons: Iterable[int],
    games: int = 17,
    n: int = 5000,
    seed: int | None = None,
) -> pd.DataFrame:
    """Full season projection per player: volume x shrunk rate, composed.

    Columns: player_id, position, proj_points, p10, p50, p90, sd.
    """
    seasons = tuple(seasons)
    weights = scoring_weights(config)
    volume = season_volume(history, seasons)

    subset = history[history["season"].isin(seasons)]
    priors = _positional_priors(subset)
    totals = subset.groupby("player_id")[list(_TOTAL_COLUMNS)].sum().reset_index()

    merged = volume.merge(totals, on="player_id", how="left").fillna(0.0)
    base_seed = _resolve_seed(seed)

    rows = []
    for _, row in merged.iterrows():
        position = row["position"]
        prior = (
            priors.loc[position] if position in priors.index
            else pd.Series({"rush_eff": 0.0, "rush_td_rate": 0.0,
                             "rec_eff": 0.0, "rec_td_rate": 0.0, "catch_rate": 0.0})
        )

        rush_eff = shrunk_rate(row["rushing_yards"], row["carries"], prior["rush_eff"], RUSH_EFF_STRENGTH)
        rush_td = shrunk_rate(row["rushing_tds"], row["carries"], prior["rush_td_rate"], RUSH_TD_STRENGTH)
        rec_eff = shrunk_rate(row["receiving_yards"], row["targets"], prior["rec_eff"], REC_EFF_STRENGTH)
        rec_td = shrunk_rate(row["receiving_tds"], row["targets"], prior["rec_td_rate"], REC_TD_STRENGTH)
        catch_rate = shrunk_rate(row["receptions"], row["targets"], prior["catch_rate"], CATCH_RATE_STRENGTH)

        # A single simulate_points call at volume = per_game * games is
        # distributionally identical to summing `games` independent weekly
        # draws at the per-game volume (Poisson opportunities and, given
        # matching opportunities, Gamma yards and Binomial touchdowns are
        # all closed under summing independent draws at a fixed rate) -- so
        # this is the exact season total, not an approximation, without a
        # per-week loop.
        rush_volume = row["carries_per_game"] * games
        rec_volume = row["targets_per_game"] * games

        player_id = row["player_id"]
        rush_seed = base_seed + _stable_seed(player_id, 0)
        rec_seed = base_seed + _stable_seed(player_id, 1)
        catch_seed = base_seed + _stable_seed(player_id, 2)

        rush_yards, rush_tds = _extract_yards_and_tds(rush_volume, rush_eff, rush_td, n, rush_seed)
        rec_yards, rec_tds = _extract_yards_and_tds(rec_volume, rec_eff, rec_td, n, rec_seed)

        # Receptions: not modelled by simulate_points at all (it only knows
        # yards and touchdowns), yet this league scores them explicitly. Its
        # opportunity count is reconstructed with the SAME seed/volume/n
        # `_extract_yards_and_tds` used for the receiving stream above --
        # the exact draw simulate_points itself makes first -- so a
        # big-target-share sample also gets more receptions, not an
        # uncorrelated count; only the catch outcome itself gets its own
        # independent randomness.
        rec_opportunities = np.random.default_rng(rec_seed).poisson(max(rec_volume, 0.0), size=n)
        receptions = np.random.default_rng(catch_seed).binomial(
            rec_opportunities, min(max(catch_rate, 0.0), 1.0)
        )

        points = (
            rush_yards * weights.get("rushing_yards", 0.0)
            + rush_tds * weights.get("rushing_tds", 0.0)
            + rec_yards * weights.get("receiving_yards", 0.0)
            + rec_tds * weights.get("receiving_tds", 0.0)
            + receptions * weights.get("receptions", 0.0)
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
        })

    return pd.DataFrame(rows, columns=["player_id", "position", "proj_points", "p10", "p50", "p90", "sd"])
