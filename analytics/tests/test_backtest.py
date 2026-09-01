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


def test_mae_uses_absolute_error_not_signed_error():
    # Errors that cancel: mean(diff) is 0.0 but mae must be 1.0. Catches a
    # regression that drops abs().
    pred, actual = np.array([1.0, -1.0]), np.array([0.0, 0.0])
    assert mae(pred, actual) == pytest.approx(1.0)


def test_rmse_takes_the_square_root_of_the_mean_squared_error():
    # diff of 2 -> MSE 4, RMSE 2. Any fixture with |diff| == 1 cannot tell
    # these apart, which is why the known-answer test does not catch it.
    pred, actual = np.array([2.0, 2.0]), np.array([0.0, 0.0])
    assert rmse(pred, actual) == pytest.approx(2.0)


def test_spearman_is_rank_based_not_linear():
    # Monotonic but strongly non-linear: Spearman is exactly 1.0 while Pearson
    # is not. Catches spearmanr being swapped for pearsonr.
    pred, actual = np.array([1.0, 4.0, 9.0, 16.0]), np.array([10.0, 20.0, 30.0, 40.0])
    assert spearman(pred, actual) == pytest.approx(1.0)
    from scipy import stats
    assert stats.pearsonr(pred, actual).statistic < 0.99


def test_spearman_is_nan_on_constant_prediction_not_zero():
    # Constant predictions carry no ordering information, so the correlation
    # is genuinely undefined -- a documented NaN, never a fabricated 0.0.
    out = spearman(np.array([5.0, 5.0, 5.0]), np.array([1.0, 2.0, 3.0]))
    assert np.isnan(out)


def test_spearman_is_nan_for_a_single_sample():
    # n == 1 has no ordering to correlate either; same documented contract.
    assert np.isnan(spearman(np.array([5.0]), np.array([1.0])))
