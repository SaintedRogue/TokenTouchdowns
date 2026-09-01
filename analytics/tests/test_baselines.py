import pandas as pd
import pytest
from tt.studies.baselines import compare_baselines


def synthetic():
    """A player whose points are a constant plus noise-free trend."""
    rows = []
    for player in ("A", "B", "C"):
        base = {"A": 5.0, "B": 10.0, "C": 15.0}[player]
        for week in range(1, 11):
            rows.append({"player_id": player, "season": 2025, "week": week,
                         "points": base})
    return pd.DataFrame(rows)


def test_compare_baselines_returns_a_row_per_baseline():
    out = compare_baselines(synthetic(), seasons=(2025,), start_week=5)
    assert set(out["baseline"]) >= {"last_3", "last_8"}


def test_a_trailing_mean_is_perfect_on_a_constant_series():
    # If the harness cannot report a perfect score on data where the baseline
    # IS the answer, the harness is broken, not the baseline.
    out = compare_baselines(synthetic(), seasons=(2025,), start_week=5)
    last3 = out.set_index("baseline").loc["last_3"]
    assert last3["mae"] == pytest.approx(0.0, abs=1e-9)
    assert last3["n"] > 0


def test_compare_baselines_reports_every_metric():
    out = compare_baselines(synthetic(), seasons=(2025,), start_week=5)
    assert {"baseline", "n", "mae", "rmse", "spearman"} <= set(out.columns)


def test_min_actual_none_matches_todays_behaviour():
    """Pin the existing (pre-min_actual) behaviour: an explicit None must be
    indistinguishable from omitting the parameter entirely."""
    without_param = compare_baselines(synthetic(), seasons=(2025,), start_week=5)
    with_explicit_none = compare_baselines(
        synthetic(), seasons=(2025,), start_week=5, min_actual=None
    )
    pd.testing.assert_frame_equal(without_param, with_explicit_none)

    # And the numbers themselves are what they were before min_actual existed:
    # synthetic() is noise-free and constant per player, so every baseline is
    # a perfect predictor.
    last8 = without_param.set_index("baseline").loc["last_8"]
    assert last8["mae"] == pytest.approx(0.0, abs=1e-9)
    assert last8["rmse"] == pytest.approx(0.0, abs=1e-9)
    assert last8["n"] == 18  # 3 players x weeks 5-10


def test_min_actual_filters_scored_rows_but_not_history():
    """Low-scoring rows must be excluded from `n`, while predictions must
    still be derived from the player's full, unfiltered trailing history --
    the exact distinction the docstring warns is easy to get backwards."""
    rows = []
    weekly_points = [1.0, 1.0, 1.0, 1.0, 9.0, 9.0, 9.0, 9.0]
    for week, points in enumerate(weekly_points, start=1):
        rows.append(
            {"player_id": "X", "season": 2025, "week": week, "points": points}
        )
    df = pd.DataFrame(rows)

    out = compare_baselines(
        df, seasons=(2025,), start_week=5, n_values=(3,), min_actual=5.0
    )
    last3 = out.set_index("baseline").loc["last_3"]

    # Only weeks 5-8 (points=9.0) clear the min_actual=5.0 threshold.
    assert last3["n"] == 4

    # If history were filtered by min_actual too, week 5's last_3 prediction
    # would have no eligible prior weeks (weeks 1-4 all score 1.0, below the
    # threshold) and the fold would either be dropped or wrongly empty.
    # Instead, predictions must be built from the full history: week 5 sees
    # weeks 2-4 (all 1.0) -> pred 1.0 vs actual 9.0 (diff 8.0); week 6 sees
    # weeks 3-5 (1.0, 1.0, 9.0) -> pred 3.6667 vs actual 9.0 (diff 5.3333);
    # week 7 sees weeks 4-6 (1.0, 9.0, 9.0) -> pred 6.3333 vs actual 9.0
    # (diff 2.6667); week 8 sees weeks 5-7 (9.0, 9.0, 9.0) -> pred 9.0 vs
    # actual 9.0 (diff 0.0). Mean of those diffs is exactly 4.0.
    assert last3["mae"] == pytest.approx(4.0, abs=1e-6)


def test_min_actual_above_every_value_yields_empty_result_not_a_crash():
    out = compare_baselines(
        synthetic(), seasons=(2025,), start_week=5, min_actual=1000.0
    )
    assert out.empty
