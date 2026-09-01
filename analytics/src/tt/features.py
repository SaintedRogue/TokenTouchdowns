"""Point-in-time feature engineering.

Every function here takes an as-of (season, week) and may only read rows
STRICTLY BEFORE it. This is structural, not conventional: `prior_weeks` is the
only gateway to history, and the leakage test asserts that truncating the future
cannot change any feature value.
"""
from __future__ import annotations

import pandas as pd

VOLUME_COLUMNS = ("carries", "targets", "receptions")


class PointInTimeError(RuntimeError):
    """A feature tried to read data at or after its as-of week."""


def prior_weeks(df: pd.DataFrame, as_of_season: int, as_of_week: int) -> pd.DataFrame:
    """Rows strictly before (as_of_season, as_of_week).

    Earlier seasons are included in full; the current season is cut at the week
    being predicted. Nothing at or after the as-of point is ever visible.
    """
    earlier_season = df["season"] < as_of_season
    same_season_earlier_week = (df["season"] == as_of_season) & (df["week"] < as_of_week)
    return df[earlier_season | same_season_earlier_week].copy()


def rolling_volume(
    df: pd.DataFrame,
    as_of_season: int,
    as_of_week: int,
    windows: tuple[int, ...] = (3, 8),
) -> pd.DataFrame:
    """Per-player mean volume over the last N weeks before the as-of point.

    Volume is what the spec's measurements say is actually predictable
    (carries r=0.825, targets r=0.623), so it is the model's real input.
    """
    history = prior_weeks(df, as_of_season, as_of_week)
    if history.empty:
        return pd.DataFrame(columns=["player_id"])

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

    return out


def shrunk_rate(
    numerator: float, denominator: float, prior: float, strength: float
) -> float:
    """Empirical-Bayes shrinkage toward `prior`.

    Used for efficiency and touchdown rates, which the spec measures as
    functionally random (yards/carry r=0.016, TDs r=0.147). `strength` is the
    pseudo-count: how many observations of the prior the estimate is worth.
    """
    return (numerator + prior * strength) / (denominator + strength)
