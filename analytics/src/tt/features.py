"""Point-in-time feature engineering.

Every function here takes an as-of (season, week) and may only read rows
STRICTLY BEFORE it. `prior_weeks` is the only function in this module
permitted to filter by season or week; every other function routes through
it rather than filtering on its own, and it self-checks its own postcondition
before returning (see PointInTimeError). That guarantee is enforced within
this module -- it cannot stop code elsewhere in the codebase from filtering a
DataFrame directly instead of calling prior_weeks, so callers everywhere must
actually route through it for the protection to apply. The leakage test
asserts that truncating the future cannot change any feature value.
"""
from __future__ import annotations

import pandas as pd

VOLUME_COLUMNS = ("carries", "targets", "receptions")


class PointInTimeError(RuntimeError):
    """Raised when prior_weeks' own point-in-time postcondition is violated.

    This is prior_weeks checking itself before it returns; it cannot catch
    code elsewhere that bypasses prior_weeks and filters a DataFrame directly.
    """


def prior_weeks(df: pd.DataFrame, as_of_season: int, as_of_week: int) -> pd.DataFrame:
    """Rows strictly before (as_of_season, as_of_week).

    Earlier seasons are included in full; the current season is cut at the week
    being predicted. This is the only function permitted to filter by
    season/week -- every other function in this module must route through it.
    That guarantee is enforced within this module (the self-check below raises
    PointInTimeError if it is ever violated), not across the whole codebase:
    nothing here stops other code from filtering a DataFrame directly instead
    of calling this function.
    """
    earlier_season = df["season"] < as_of_season
    same_season_earlier_week = (df["season"] == as_of_season) & (df["week"] < as_of_week)
    result = df[earlier_season | same_season_earlier_week].copy()

    # Self-check: verify the postcondition this function exists to provide,
    # rather than merely asserting it in the docstring above. Cheap relative
    # to the cost of a leak that would make every downstream evaluation
    # number optimistic and invisible. Covers both leak vectors: a future
    # season slipping through, and a same-season row at or after the as-of
    # week slipping through.
    if not result.empty:
        leaked_future_seasons = result.loc[result["season"] > as_of_season, "season"]
        if not leaked_future_seasons.empty:
            raise PointInTimeError(
                f"prior_weeks leaked season {int(leaked_future_seasons.max())} "
                f"at as-of {as_of_season} wk{as_of_week}"
            )
        same_season_weeks = result.loc[result["season"] == as_of_season, "week"]
        if not same_season_weeks.empty and same_season_weeks.max() >= as_of_week:
            raise PointInTimeError(
                f"prior_weeks leaked week {int(same_season_weeks.max())} "
                f"at as-of {as_of_season} wk{as_of_week}"
            )
    return result


def rolling_volume(
    df: pd.DataFrame,
    as_of_season: int,
    as_of_week: int,
    windows: tuple[int, ...] = (3, 8),
) -> pd.DataFrame:
    """Per-player mean volume over the last N weeks before the as-of point.

    Volume is what the spec's measurements say is actually predictable
    (carries r=0.825, targets r=0.623), so it is the model's real input.
    receptions is included alongside them on the same volume-is-sticky
    reasoning; the spec does not report its autocorrelation separately.
    """
    # Both branches below must agree on this column set -- deriving it once
    # here, instead of hardcoding a shorter column list in the empty-history
    # return, is what keeps the empty and populated paths from drifting apart.
    columns = ["player_id"] + [
        f"{c}_r{window}" for window in windows for c in VOLUME_COLUMNS
    ]

    history = prior_weeks(df, as_of_season, as_of_week)
    if history.empty:
        return pd.DataFrame(columns=columns)

    # Sort chronologically across seasons, not just by week within a season --
    # dropping "season" here would let a late week from an old season outrank
    # an early week from a new one, and .tail(window) would silently average
    # the wrong rows.
    history = history.sort_values(["player_id", "season", "week"])
    out = history[["player_id"]].drop_duplicates().reset_index(drop=True)

    for window in windows:
        tail = (
            history.groupby("player_id", group_keys=False)
            .tail(window)
            .groupby("player_id")[list(VOLUME_COLUMNS)]
            .mean()
            .rename(columns={c: f"{c}_r{window}" for c in VOLUME_COLUMNS})
            .reset_index()
        )
        out = out.merge(tail, on="player_id", how="left")

    return out[columns]


def shrunk_rate(
    numerator: float, denominator: float, prior: float, strength: float
) -> float:
    """Empirical-Bayes shrinkage toward `prior`.

    Used for efficiency and touchdown rates, which the spec measures as
    functionally random (yards/carry r=0.016, TDs r=0.147). `strength` is the
    pseudo-count: how many observations of the prior the estimate is worth.
    """
    total_weight = denominator + strength
    if total_weight == 0:
        # No observations and no prior weight (e.g. a rookie or inactive
        # player scored with strength=0): there is nothing to compute an
        # estimate from, so the prior is the least-wrong answer -- not a
        # ZeroDivisionError or a silent nan/inf.
        return prior
    return (numerator + prior * strength) / total_weight
