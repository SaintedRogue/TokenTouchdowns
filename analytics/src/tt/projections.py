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

THE POINT SUM IS DRIVEN FROM `scoring_weights(config)`, NOT A HARDCODED TERM
LIST. An earlier version summed eight named terms directly, which meant a
scored stat this module had a weight for but no line of code for (fumbles
lost, in the real league's own scoring) was silently worth zero in every
projection -- the same shape of bug as the passing/defensive-interception
stat-id collision `league.scoring_weights` itself exists to prevent (see its
module docstring). `_validate_scoring_is_simulated` checks, once, that every
key `scoring_weights` can produce has a simulated component to be weighted;
if a league scores something this module genuinely cannot simulate, it
raises rather than quietly dropping that rule from every player's number.

That guarantee used to stop at `league.py`'s own front door: `Ret TD` and
`2-PT` are stats an offensive skill player can score, and nflverse carries
columns for both, but `league.scoring_weights` had no entry for either, so
they never reached `scoring_weights(config)` for `_validate_scoring_is_
simulated` to see in the first place -- a silent drop hiding behind a
"fails loud" promise. `_warn_about_unmodellable_scoring` closes that,
via `league.missing_scored_columns`: a WARNING (not a raise, since these
have no simulated component to add rather than a wiring bug to fix, and the
real league this project targets scores both) the moment a league scores
something this module drops for a reason other than "obviously a K/DEF
stat this pipeline was never going to project" (see F7 in
fix-round-1-brief.md).

ONLY QB/RB/WR/TE (`PROJECTABLE_POSITIONS`) ARE PROJECTED. Kicker and team
defense scoring comes from columns this pipeline never ingests (field goals
by distance, points allowed, sacks, forced turnovers); deriving a number for
either from offensive columns would look like a real projection and not be
one. See that constant's own comment for the full reasoning.
"""
from __future__ import annotations

import warnings
import zlib
from collections.abc import Iterable

import numpy as np
import pandas as pd

from .features import shrunk_rate
from .league import LeagueConfig, missing_scored_columns, scoring_weights
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

# Fumbles lost are rarer and noisier than even touchdowns -- a player who
# fumbled twice in twenty carries is not a 10%-per-touch fumbler -- so these
# sit at the high end of the strength range, comparable to the passing
# constants above. sack_fumbles_lost is shrunk per PASS ATTEMPT (not a
# dedicated sack/dropback count, which this module has no volume model for
# at all): attempts is the closest available proxy for "how many passing
# plays put this player at sack risk".
RUSH_FUMBLE_STRENGTH = 400.0
REC_FUMBLE_STRENGTH = 400.0
SACK_FUMBLE_STRENGTH = 600.0

# Expected games played per season: NOT a rate over opportunities like the
# constants above, but the same shrinkage mechanics apply directly if it's
# framed as one -- "games per season", shrunk toward a positional prior, with
# each player's own recency-weighted season count standing in for the
# denominator (see `_games_evidence` and season_volume's `_combine`).
#
# A single unit here is worth ONE MOST-RECENT SEASON of evidence. That is now
# literally true, because `_games_evidence` normalises the recency weights to
# effective seasons; it was previously FALSE for any window other than two
# seasons, and that discrepancy was the bug. The raw weight total grows
# geometrically with the number of seasons a caller lists (2 -> 4, 3 -> 13,
# 5 -> 121, 7 -> 1093), so against a fixed strength the prior's share of the
# estimate slid from 50% down to 0.4% purely from how much ancient,
# near-zero-weight history the caller happened to pass. Josh Allen projected
# 12.18 games on `seasons=(2024, 2025)` and 16.05 on `(2019..2025)` with his
# weighted per-game volume identical to two decimal places.
#
# The VALUE is the old 4.0 restated in the new units, not a re-tuning: the
# normaliser for a two-season window is the maximum weight, 3, so 4.0/3
# reproduces the previously-calibrated two-season behaviour EXACTLY (pinned
# by test_two_season_proj_games_is_unchanged_by_the_evidence_normalisation)
# while removing the window dependence everywhere else. In effective-seasons
# terms it reads as: the positional prior is worth about one and a third
# recent seasons of a player's own observed availability -- so a lone recent
# season is trusted somewhat, and a lone OLD season (normalised weight 1/3 or
# less) is pulled hard toward the prior, which is what the original comment
# claimed all along.
GAMES_STRENGTH = 4.0 / 3.0

# The real NFL regular-season length. proj_games can never legitimately
# exceed this -- it caps both a shrunk estimate that lands above it (rare)
# and any data quirk (extra logged weeks) in the input.
SEASON_LENGTH = 17.0

# This module projects offensive skill-position volume (carries, targets,
# pass attempts) and the rates that convert them to points. Kicker and team
# defense scoring is built from an entirely different set of columns this
# pipeline never ingests at all -- field goals made by distance, points
# allowed, sacks, forced turnovers -- so there is no volume/efficiency model
# here that could honestly project them. Deriving a K or DEF number from
# offensive columns anyway is exactly the bug this constant fixes: a KICKER
# projected off an apparent trick-play rushing/receiving line, a number that
# LOOKS like a real projection and is not. Both positions are drafted in the
# final rounds by convention and their value-over-replacement spread is
# nearly flat there regardless -- an honest absence from this module's
# output serves a draft board better than a fabricated ranking. Framed as an
# ALLOWLIST (not a K/DEF denylist) so every other non-skill position
# nflverse's per-player stats happen to include (offensive line, individual
# defensive players, punters, long-snappers) is excluded the same way,
# without having to name each one.
PROJECTABLE_POSITIONS = frozenset({"QB", "RB", "WR", "TE"})

# Every stat column this module reads. A `history` frame that doesn't track a
# stream at all (e.g. a skill-position-only fixture with no passing columns)
# is filled to zero for the columns it's missing, rather than raising --
# see `_with_required_columns`.
_REQUIRED_STAT_COLUMNS = (
    "carries", "rushing_yards", "rushing_tds", "rushing_fumbles_lost",
    "targets", "receiving_yards", "receiving_tds", "receptions", "receiving_fumbles_lost",
    "attempts", "passing_yards", "passing_tds", "passing_interceptions", "sack_fumbles_lost",
)

# The full, fixed set of nflverse stat columns this module ever simulates a
# points component for -- i.e. every key `points` (in project_players) can
# legally weight. Checked against `scoring_weights(config)` up front (see
# `_validate_scoring_is_simulated`) so a league scoring a column outside this
# set fails loudly instead of that rule being silently dropped from every
# projection, which is exactly how the fumbles-lost bug this constant fixes
# happened: `league.scoring_weights()` correctly derived
# rushing/receiving/sack_fumbles_lost weights, and the point formula simply
# never looked at them.
_SIMULATED_COMPONENTS = frozenset({
    "rushing_yards", "rushing_tds", "rushing_fumbles_lost",
    "receiving_yards", "receiving_tds", "receptions", "receiving_fumbles_lost",
    "passing_yards", "passing_tds", "passing_interceptions", "sack_fumbles_lost",
})

# nflverse's `stats_player_week_{season}.parquet` -- the asset `ingest.py`
# pins and this module's real caller loads -- carries BOTH regular-season and
# postseason rows, tagged by `season_type` in {"REG", "POST"} at weeks 1-18
# and 19-22 respectively (882 of 19,422 rows in 2025). Fantasy football is
# scored over the regular season only, so every postseason row is a row from
# a different population than the one being projected, and counting them:
#
#   - inflates `games` (a playoff run reads as a 21-game season, which
#     SEASON_LENGTH then clamps back to 17 -- CONCEALING the inflation
#     rather than preventing it),
#   - blends January usage into a regular-season per-game rate, and
#   - contaminates the positional efficiency/TD-rate priors.
#
# Critically, the bias is not noise: only good teams play in January, so the
# inflation is CORRELATED WITH TEAM QUALITY and therefore looks exactly like
# signal (measured: Josh Allen +16.9% projected points, Stafford +16.6%,
# Chase +0.8% -- enough to reorder the delivered draft board).
REGULAR_SEASON_TYPE = "REG"

_EMPTY_PRIOR = pd.Series({
    "rush_eff": 0.0, "rush_td_rate": 0.0, "rush_fumble_rate": 0.0,
    "rec_eff": 0.0, "rec_td_rate": 0.0, "catch_rate": 0.0, "rec_fumble_rate": 0.0,
    "pass_eff": 0.0, "pass_td_rate": 0.0, "pass_int_rate": 0.0, "sack_fumble_rate": 0.0,
})


class NoProjectableDataError(ValueError):
    """Raised when `project_players` has no player-weeks left to compute a
    positional prior from once `history` is filtered down to
    `PROJECTABLE_POSITIONS` and the requested `seasons` -- an empty
    `history` to begin with, a frame containing only non-projectable
    positions (e.g. a kicker-only frame), or a `seasons=` window that
    matches no rows at all (e.g. a pre-season call before any games in that
    season exist).

    Replaces `KeyError: "None of ['position'] are in the columns"` --
    `pd.DataFrame([]).set_index('position')`'s genuinely correct behaviour
    for zero input rows, but a message that names neither the real cause
    nor which function hit it (see F8 in fix-round-1-brief.md).
    """


def _validate_scoring_is_simulated(weights: dict[str, float]) -> None:
    """Fail loudly if the league scores a stat this module cannot simulate.

    scoring_weights(config) is trusted as the league's real scoring, per this
    module's own docstring ("never the hardcoded HALF_PPR") -- but trusting
    it as an INPUT does not mean every key it can ever emit is actually wired
    into the point formula below. That gap is exactly how the fumbles-lost
    bug happened: `scoring_weights` correctly returned
    rushing_fumbles_lost/receiving_fumbles_lost/sack_fumbles_lost weights,
    and the point formula, hardcoded to eight named terms, simply never
    looked at them. Checked once here, against `_SIMULATED_COMPONENTS` --
    the full, fixed set of stats this module ever simulates a component for
    -- rather than per player, since which stats are simulated is a property
    of this module's code, not of any one player's history.
    """
    unmapped = sorted(set(weights) - _SIMULATED_COMPONENTS)
    if unmapped:
        raise ValueError(
            f"league scoring includes {unmapped}, which projections.py has no "
            "simulated component for -- add a stream/shrinkage for it rather "
            "than silently dropping it from every projection (see "
            "_SIMULATED_COMPONENTS)."
        )


def _warn_about_unmodellable_scoring(config: LeagueConfig) -> None:
    """Surface, rather than silently drop, any league-scored stat this
    module cannot simulate for a reason `league.py` itself can't already
    explain as "an intentionally-excluded K/DEF stat" -- see
    `league.missing_scored_columns` (F7 in fix-round-1-brief.md).

    `_validate_scoring_is_simulated` (above) only ever sees
    `scoring_weights(config)` -- a dict `league.py` has ALREADY dropped
    unmapped stat ids from -- so a stat this module never learns about at
    all (Ret TD, 2-PT: nflverse has the columns, but this module has no
    volume/rate model for either) never reached that check, and the
    module's "fail loud, not quietly drop it" promise was only true
    downstream of `league.py`'s own silent drop. This closes that gap with
    a WARNING, not a raise: unlike the fumbles-lost bug class
    `_validate_scoring_is_simulated` guards (a genuine wiring bug -- the
    stat HAS a simulated component and the point formula just wasn't
    summing it), `missing_scored_columns` covers stats with NO simulated
    component at all. Raising would make every call against a league that
    scores either stat (the real league this project targets does) fail
    outright over a genuinely small gap (10-20 points over two seasons,
    per the fix brief) rather than a fixable wiring bug -- a warning is
    what makes the gap impossible to miss without also making the module
    unusable for the league it exists to serve.
    """
    missing = missing_scored_columns(config)
    if missing:
        stats = sorted({f"{stat['name']!r} (statId {stat['statId']})" for stat in missing})
        warnings.warn(
            f"league scores {stats} but projections.py has no simulated "
            "component for them (see league.missing_scored_columns) -- "
            "every projection is silently missing this point value.",
            stacklevel=2,
        )


def regular_season(history: pd.DataFrame) -> pd.DataFrame:
    """`history` restricted to regular-season rows -- see
    `REGULAR_SEASON_TYPE` for why postseason rows are a different population.

    Applied at the TOP of both public entry points (`season_volume` and
    `project_players`), before any aggregation, so no caller can reach an
    aggregate computed over the wrong rows. Idempotent, so the fact that
    `project_players` both filters and calls `season_volume` (which filters
    again) costs nothing.

    A frame with NO `season_type` column at all passes through unchanged.
    That is not an assumption about unknown data: it is the documented
    contract for a hand-built frame that never had postseason rows to begin
    with (every fixture in this module's test suite, and any caller
    assembling its own history). The place where a MISSING column would be
    alarming is ingestion -- an nflverse asset that stopped carrying
    `season_type` would mean the release layout changed underneath us -- and
    that is exactly where it raises instead; see `ingest.load_seasons`.
    """
    if "season_type" not in history.columns:
        return history
    return history[history["season_type"] == REGULAR_SEASON_TYPE]


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


def _weight_scale(recency_weights: dict[int, float]) -> float:
    """The normaliser that turns raw recency weights into effective seasons:
    the weight of the MOST RECENT season in the window.

    Anchoring on the maximum (rather than, say, the sum) is what makes the
    result scale-invariant. Under a geometric scheme the weight of a season
    `k` years before the newest is `ratio ** -k` once divided by the maximum,
    which depends only on how old that season is -- never on how many even
    older seasons the caller also listed. Falls back to 1.0 when no weight is
    positive, so the caller's own units pass through untouched rather than
    dividing by zero.
    """
    scale = max(recency_weights.values(), default=0.0)
    return float(scale) if scale > 0 else 1.0


def _games_evidence(
    season_weights: Iterable[float], recency_weights: dict[int, float]
) -> float:
    """How many EFFECTIVE SEASONS of availability evidence a player carries.

    `season_weights` are that one player's own per-season recency weights.
    Dividing their total by `_weight_scale` expresses the result in units of
    "most-recent seasons", which is the unit `GAMES_STRENGTH` is defined in
    (see that constant). A player present in every season of the window has
    an effective sample size in [1, ratio/(ratio-1)) -- bounded at 1.5 under
    the default 3x scheme no matter how deep the history goes -- because a
    season the weighting scheme has already declared near-worthless for
    VOLUME cannot simultaneously count as strong evidence of AVAILABILITY.
    The raw total, by contrast, grows without limit (4, 13, 121, 1093, ...),
    which is what made the prior's influence depend on the caller's window.
    """
    return float(sum(season_weights)) / _weight_scale(recency_weights)


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
    history = regular_season(history)
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
            # Numerator and evidence are normalised by the SAME scale, so
            # the blend below is a weighted average in effective-season
            # units -- the units GAMES_STRENGTH is defined in. Normalising
            # only one of the two would rescale the estimate itself rather
            # than the prior's influence on it.
            scale = _weight_scale(recency_weights)
            weighted_games = float((group["games"] * group["weight"]).sum()) / scale
            games_evidence = _games_evidence(group["weight"], recency_weights)
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

    Raises `NoProjectableDataError` on an empty `subset` -- e.g. a kicker-
    only `history` (nothing survives the `PROJECTABLE_POSITIONS` filter) or
    a `seasons=` window matching no rows at all -- rather than letting
    `pd.DataFrame([]).set_index('position')` raise its own generic,
    uninformative `KeyError` a few lines below (see F8 in
    fix-round-1-brief.md).
    """
    if subset.empty:
        raise NoProjectableDataError(
            "no player-weeks to compute a positional prior from -- history "
            "has no rows for a PROJECTABLE_POSITIONS position within the "
            "requested seasons (an empty history, a kicker/DEF-only frame, "
            "or a seasons= window with no matching rows all land here)"
        )
    rows = []
    for position, group in subset.groupby("position"):
        rows.append({
            "position": position,
            "rush_eff": _rate(group["rushing_yards"], group["carries"]),
            "rush_td_rate": _rate(group["rushing_tds"], group["carries"]),
            "rush_fumble_rate": _rate(group["rushing_fumbles_lost"], group["carries"]),
            "rec_eff": _rate(group["receiving_yards"], group["targets"]),
            "rec_td_rate": _rate(group["receiving_tds"], group["targets"]),
            "catch_rate": _rate(group["receptions"], group["targets"]),
            "rec_fumble_rate": _rate(group["receiving_fumbles_lost"], group["targets"]),
            "pass_eff": _rate(group["passing_yards"], group["attempts"]),
            "pass_td_rate": _rate(group["passing_tds"], group["attempts"]),
            "pass_int_rate": _rate(group["passing_interceptions"], group["attempts"]),
            "sack_fumble_rate": _rate(group["sack_fumbles_lost"], group["attempts"]),
        })
    return pd.DataFrame(rows).set_index("position")


def _resolve_names(history: pd.DataFrame) -> pd.Series:
    """Per-player display name for the board, indexed by player_id.

    A draft board keyed on nflverse ids like `00-0030506` is unreadable at a
    live draft with 60 seconds per pick -- this is the whole reason this
    function exists. Prefers `player_display_name`; falls back, per row, to
    `player_name` where the display name is missing; and falls back further
    to the player_id itself when `history` carries NEITHER column at all
    (true of every fixture in this module's own test suite, and possibly of
    a caller with a slimmer history frame) -- the least-wrong default (same
    principle as `season_volume`'s own `_combine` fallback), not a missing
    column or a NaN that would break a board's display.

    An empty string is treated the same as a missing value at each fallback
    step -- `fillna` alone leaves `""` in place (it is not NaN), which would
    silently ship a blank name to the board instead of falling back to
    `player_name` or the player_id.

    Picks the MOST RECENT season's name per player, not whichever row
    `history` happens to list first: a plain `.first()` over an unsorted
    frame returns the OLDEST name after a real player rename (a legal name
    change, a display-name correction) and would flip depending on how
    `history` happened to be concatenated -- sorting by `season` first
    makes "most recent" well-defined regardless of input row order.
    """
    name = history.get("player_display_name")
    if name is None:
        name = pd.Series(pd.NA, index=history.index, dtype="object")
    else:
        name = name.replace("", pd.NA)
    player_name = history.get("player_name")
    if player_name is not None:
        name = name.fillna(player_name.replace("", pd.NA))
    name = name.fillna(history["player_id"])
    ordered = history.assign(_name=name).sort_values("season")
    return ordered.groupby("player_id")["_name"].last()


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

    Columns: player_id, name, position, proj_points, p10, p50, p90, sd,
    proj_games. `name` is carried through from `history`'s
    `player_display_name` (falling back to `player_name`, then to the
    player_id itself -- see `_resolve_names`) purely for board readability;
    it plays no role in any projection. Only `PROJECTABLE_POSITIONS`
    (QB/RB/WR/TE) appear in the output -- see that constant's comment for
    why kickers and team defenses are deliberately excluded rather than
    mismodelled.

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
    _validate_scoring_is_simulated(weights)
    _warn_about_unmodellable_scoring(config)

    history = regular_season(history)
    history = _with_required_columns(history)
    history = history[history["position"].isin(PROJECTABLE_POSITIONS)]
    names = _resolve_names(history)
    volume = season_volume(history, seasons)

    subset = history[history["season"].isin(seasons)]
    priors = _positional_priors(subset)
    totals = subset.groupby("player_id")[list(_REQUIRED_STAT_COLUMNS)].sum().reset_index()

    merged = volume.merge(totals, on="player_id", how="left").fillna(0.0)
    # Attached after fillna(0.0) above -- `names` is indexed by every
    # player_id in the position-filtered `history` (see `_resolve_names`),
    # a SUPERSET of the player_ids `volume`/`merged` contain (`volume`
    # additionally restricts to `seasons`; `names` deliberately doesn't, so
    # a player rename in an OLDER season out of that window still resolves
    # correctly). `.map` is a key-based lookup, so the extra names `names`
    # carries for players outside this projection's season window are
    # simply never looked up -- harmless, and not a fillna coercion.
    merged["name"] = merged["player_id"].map(names)
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
        rush_fumble = shrunk_rate(row["rushing_fumbles_lost"], row["carries"], prior["rush_fumble_rate"], RUSH_FUMBLE_STRENGTH)
        rec_eff = max(shrunk_rate(row["receiving_yards"], row["targets"], prior["rec_eff"], REC_EFF_STRENGTH), 0.0)
        rec_td = shrunk_rate(row["receiving_tds"], row["targets"], prior["rec_td_rate"], REC_TD_STRENGTH)
        catch_rate = shrunk_rate(row["receptions"], row["targets"], prior["catch_rate"], CATCH_RATE_STRENGTH)
        rec_fumble = shrunk_rate(row["receiving_fumbles_lost"], row["targets"], prior["rec_fumble_rate"], REC_FUMBLE_STRENGTH)
        pass_eff = max(shrunk_rate(row["passing_yards"], row["attempts"], prior["pass_eff"], PASS_EFF_STRENGTH), 0.0)
        pass_td = shrunk_rate(row["passing_tds"], row["attempts"], prior["pass_td_rate"], PASS_TD_STRENGTH)
        pass_int = shrunk_rate(row["passing_interceptions"], row["attempts"], prior["pass_int_rate"], PASS_INT_STRENGTH)
        sack_fumble = shrunk_rate(row["sack_fumbles_lost"], row["attempts"], prior["sack_fumble_rate"], SACK_FUMBLE_STRENGTH)

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
        rush_fumble_seed = base_seed + _stable_seed(player_id, 5)
        rec_fumble_seed = base_seed + _stable_seed(player_id, 6)
        sack_fumble_seed = base_seed + _stable_seed(player_id, 7)

        rush_opportunities, rush_yards, rush_tds = simulate_components(rush_volume, rush_eff, rush_td, n=n, seed=rush_seed)
        rec_opportunities, rec_yards, rec_tds = simulate_components(rec_volume, rec_eff, rec_td, n=n, seed=rec_seed)
        pass_opportunities, pass_yards, pass_tds = simulate_components(pass_volume, pass_eff, pass_td, n=n, seed=pass_seed)

        # Receptions, interceptions and fumbles-lost: not modelled by
        # simulate_components at all (it only knows yards and touchdowns),
        # yet this league scores all of them explicitly. Each is drawn from
        # the opportunity count simulate_components handed back for its own
        # stream -- not reconstructed or re-derived -- so a big-target-share
        # (or big-attempts) sample also gets more receptions/turnovers, not
        # an uncorrelated count; only the event outcome itself gets its own
        # independent randomness.
        receptions = np.random.default_rng(catch_seed).binomial(
            rec_opportunities, min(max(catch_rate, 0.0), 1.0)
        )
        interceptions = np.random.default_rng(int_seed).binomial(
            pass_opportunities, min(max(pass_int, 0.0), 1.0)
        )
        rush_fumbles = np.random.default_rng(rush_fumble_seed).binomial(
            rush_opportunities, min(max(rush_fumble, 0.0), 1.0)
        )
        rec_fumbles = np.random.default_rng(rec_fumble_seed).binomial(
            rec_opportunities, min(max(rec_fumble, 0.0), 1.0)
        )
        sack_fumbles = np.random.default_rng(sack_fumble_seed).binomial(
            pass_opportunities, min(max(sack_fumble, 0.0), 1.0)
        )

        # Driven FROM the league's own scoring_weights, not a hardcoded list
        # of terms -- see _validate_scoring_is_simulated's docstring for why
        # a fixed term list is exactly the bug this replaced (fumbles-lost
        # weights existed and were silently never summed). `components`
        # covers every key `_SIMULATED_COMPONENTS` promises; every key in
        # `weights` is guaranteed (by the validation above) to be one of
        # them, so this can never KeyError, and a stat the league doesn't
        # score simply never gets added, same as the old `.get(..., 0.0)`.
        components = {
            "rushing_yards": rush_yards, "rushing_tds": rush_tds, "rushing_fumbles_lost": rush_fumbles,
            "receiving_yards": rec_yards, "receiving_tds": rec_tds, "receptions": receptions,
            "receiving_fumbles_lost": rec_fumbles,
            "passing_yards": pass_yards, "passing_tds": pass_tds,
            "passing_interceptions": interceptions, "sack_fumbles_lost": sack_fumbles,
        }
        # `_SIMULATED_COMPONENTS` is a promise about this dict; keeping the
        # two in lockstep here means a component added to one and forgotten
        # in the other fails immediately instead of becoming a silently
        # unscored stat -- the same failure mode the constant exists to
        # prevent. This is a structural check only: that a key holds the
        # RIGHT array is not checkable here at all, and is pinned instead by
        # test_each_scoring_component_actually_reaches_proj_points.
        if set(components) != _SIMULATED_COMPONENTS:
            raise AssertionError(
                "components and _SIMULATED_COMPONENTS have drifted apart: "
                f"{sorted(set(components) ^ _SIMULATED_COMPONENTS)}"
            )

        points = np.zeros(n)
        for stat, weight in weights.items():
            points = points + components[stat] * weight
        summary = summarise(points)
        rows.append({
            "player_id": player_id,
            "name": row["name"],
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
        columns=["player_id", "name", "position", "proj_points", "p10", "p50", "p90", "sd", "proj_games"],
    )
