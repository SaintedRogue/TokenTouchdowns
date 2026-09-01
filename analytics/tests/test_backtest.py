import numpy as np
import pandas as pd
import pytest
from tt.backtest import walk_forward, baseline_last_n, mae, rmse, spearman, evaluate


def frame():
    rows = []
    for season in (2024, 2025):
        for week in range(1, 4):
            rows.append({"player_id": "A", "season": season, "week": week,
                         "points": 10.0 + week})
    return pd.DataFrame(rows)


def test_walk_forward_yields_folds_in_chronological_order():
    folds = list(walk_forward(frame(), start_season=2025, start_week=2))
    assert folds == [(2025, 2), (2025, 3)]


def test_walk_forward_never_yields_a_fold_before_the_start():
    folds = list(walk_forward(frame(), start_season=2025, start_week=2))
    assert all((s, w) >= (2025, 2) for s, w in folds)


def test_baseline_last_n_averages_only_prior_weeks():
    # as_of 2025 wk3, n=2 -> weeks 1,2 -> points 11,12 -> 11.5
    out = baseline_last_n(frame(), 2025, 3, n=2)
    assert out.set_index("player_id").loc["A", "pred"] == pytest.approx(11.5)


def test_mae_and_rmse_are_zero_for_a_perfect_prediction():
    a = np.array([1.0, 2.0, 3.0])
    assert mae(a, a) == 0.0
    assert rmse(a, a) == 0.0


def test_rmse_punishes_a_single_large_error_more_than_mae():
    actual = np.array([0.0, 0.0, 0.0, 0.0])
    pred = np.array([0.0, 0.0, 0.0, 8.0])
    assert rmse(pred, actual) > mae(pred, actual)


def test_spearman_is_one_for_a_perfectly_ordered_prediction():
    assert spearman(np.array([1.0, 2.0, 3.0]), np.array([10.0, 20.0, 30.0])) == pytest.approx(1.0)


def test_spearman_is_negative_one_when_the_order_is_reversed():
    assert spearman(np.array([3.0, 2.0, 1.0]), np.array([10.0, 20.0, 30.0])) == pytest.approx(-1.0)


def test_evaluate_reports_every_metric_and_the_sample_size():
    out = evaluate(np.array([1.0, 2.0, 3.0]), np.array([1.0, 2.0, 4.0]))
    assert set(out) == {"n", "mae", "rmse", "spearman"}
    assert out["n"] == 3


def test_evaluate_on_a_synthetic_series_with_a_known_answer():
    # A harness that cannot recover a known answer cannot validate a model.
    actual = np.array([2.0, 4.0, 6.0, 8.0])
    pred = actual + 1.0                      # constant bias of exactly 1.0
    out = evaluate(pred, actual)
    assert out["mae"] == pytest.approx(1.0)
    assert out["rmse"] == pytest.approx(1.0)
    assert out["spearman"] == pytest.approx(1.0)
