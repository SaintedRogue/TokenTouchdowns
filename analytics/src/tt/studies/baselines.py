"""Baseline comparison over real weeks.

Spec §4.3: a model must beat these to justify existing. Reporting them honestly
is the point -- if the trailing average wins, that is the finding.
"""
from __future__ import annotations

import pandas as pd

from ..backtest import baseline_last_n, evaluate, walk_forward


def compare_baselines(
    df: pd.DataFrame,
    seasons: tuple[int, ...],
    start_week: int = 5,
    n_values: tuple[int, ...] = (3, 8),
) -> pd.DataFrame:
    """Walk forward through every week, scoring each trailing-mean baseline."""
    subset = df[df["season"].isin(seasons)]
    results: dict[str, list[tuple[float, float]]] = {f"last_{n}": [] for n in n_values}

    for season, week in walk_forward(subset, min(seasons), start_week):
        actual = subset[(subset["season"] == season) & (subset["week"] == week)]
        if actual.empty:
            continue
        for n in n_values:
            pred = baseline_last_n(subset, season, week, n=n)
            merged = actual.merge(pred, on="player_id", how="inner")
            if merged.empty:
                continue
            results[f"last_{n}"].extend(
                zip(merged["pred"].tolist(), merged["points"].tolist())
            )

    rows = []
    for name, pairs in results.items():
        if not pairs:
            continue
        preds = pd.Series([p for p, _ in pairs]).to_numpy()
        actuals = pd.Series([a for _, a in pairs]).to_numpy()
        rows.append({"baseline": name, **evaluate(preds, actuals)})
    return pd.DataFrame(rows)
