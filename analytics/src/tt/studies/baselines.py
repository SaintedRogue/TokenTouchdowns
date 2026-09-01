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

    THE TRAP: do NOT try to get a "starter-relevant" slice by pre-filtering
    ``df`` before it is passed in here (e.g. ``compare_baselines(df[df["points"]
    >= 5], ...)``). That looks equivalent but is not -- it restricts a
    player's trailing history to their own high-scoring weeks before any
    prediction is made, so the baseline ends up predicting a good week from
    an average of that player's *other good weeks*. That is lookahead bias:
    using knowledge correlated with the outcome to build the predictor, the
    exact failure mode `features.prior_weeks`'s point-in-time guard exists to
    prevent elsewhere in this codebase. This is not hypothetical -- it is
    exactly how the original (pre-`min_actual`) starter-relevant figure in
    the design doc was produced. The effect there was mixed, not uniform: it
    artificially improved MAE and RMSE (predictions pulled toward each
    player's own ceiling landed closer to the high-scoring actuals being
    scored) while artificially worsening Spearman (that same pull compresses
    players together, destroying the between-player ordering rank
    correlation measures) -- do not assume the error pushes a metric in any
    particular direction; the point is that it measured the wrong thing.
    Always pass the full, unfiltered frame and use
    `min_actual` to filter what gets scored, never what gets predicted from.
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
