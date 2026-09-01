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
