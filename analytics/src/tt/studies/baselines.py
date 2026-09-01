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
    min_actual: float | None = None,
) -> pd.DataFrame:
    """Walk forward through every week, scoring each trailing-mean baseline.

    ``min_actual``, when set, restricts the rows *scored* to those whose
    ACTUAL points meet the threshold -- e.g. ``min_actual=5.0`` reproduces the
    spec's §4.3 "starter-relevant" pool (weeks a player scored enough to be a
    plausible starter), as opposed to the full pool that includes bench
    players whose near-zero trailing average trivially predicts their
    near-zero output.

    The filter applies ONLY to the actuals being scored, never to the
    trailing history a prediction is built from: `baseline_last_n` is always
    given the full, unfiltered `subset`, and `min_actual` is applied to the
    `actual` frame afterward, inside the fold loop. A player's last-N average
    must be computed from all of their weeks, not only their good ones --
    filtering the history too would let a player's own excluded weeks bias
    their own trailing average, which is a different (and wrong) measurement
    from "can the model rank players who are actually playable this week."
    """
    subset = df[df["season"].isin(seasons)]
    results: dict[str, list[tuple[float, float]]] = {f"last_{n}": [] for n in n_values}

    for season, week in walk_forward(subset, min(seasons), start_week):
        actual = subset[(subset["season"] == season) & (subset["week"] == week)]
        if min_actual is not None:
            actual = actual[actual["points"] >= min_actual]
        if actual.empty:
            continue
        for n in n_values:
            # `subset` here is always the full, unfiltered history -- see the
            # docstring above. Only `actual` (above) is ever filtered.
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
