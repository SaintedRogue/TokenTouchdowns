"""Walk-forward backtesting.

Deliberately built before any model. Its baselines are the gate: a model that
cannot beat a trailing average or ADP order is worse than the data already in
hand, and should not ship.
"""
from __future__ import annotations

from collections.abc import Iterator

import numpy as np
import pandas as pd
from scipy import stats

from .features import prior_weeks


def walk_forward(
    df: pd.DataFrame, start_season: int, start_week: int
) -> Iterator[tuple[int, int]]:
    """Yield (season, week) folds in chronological order from the start point."""
    pairs = (
        df[["season", "week"]].drop_duplicates().sort_values(["season", "week"])
    )
    for season, week in pairs.itertuples(index=False):
        if (season, week) >= (start_season, start_week):
            yield int(season), int(week)


def baseline_last_n(
    df: pd.DataFrame, as_of_season: int, as_of_week: int, n: int = 3,
    value_column: str = "points",
) -> pd.DataFrame:
    """Trailing mean of the last n weeks. The baseline a model must beat."""
    history = prior_weeks(df, as_of_season, as_of_week)
    if history.empty:
        return pd.DataFrame(columns=["player_id", "pred"])
    history = history.sort_values(["player_id", "season", "week"])
    return (
        history.groupby("player_id", group_keys=False)
        .tail(n)
        .groupby("player_id")[value_column]
        .mean()
        .reset_index()
        .rename(columns={value_column: "pred"})
    )


def mae(pred: np.ndarray, actual: np.ndarray) -> float:
    return float(np.mean(np.abs(np.asarray(pred) - np.asarray(actual))))


def rmse(pred: np.ndarray, actual: np.ndarray) -> float:
    return float(np.sqrt(np.mean((np.asarray(pred) - np.asarray(actual)) ** 2)))


def spearman(pred: np.ndarray, actual: np.ndarray) -> float:
    """Rank correlation. For lineup decisions, order matters more than level."""
    return float(stats.spearmanr(pred, actual).statistic)


def evaluate(pred: np.ndarray, actual: np.ndarray) -> dict[str, float]:
    return {
        "n": int(len(pred)),
        "mae": mae(pred, actual),
        "rmse": rmse(pred, actual),
        "spearman": spearman(pred, actual),
    }
