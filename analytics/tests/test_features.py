import pandas as pd
import pytest
from tt.features import prior_weeks, rolling_volume, shrunk_rate


def frame():
    # One player, weeks 1-5, rising usage.
    return pd.DataFrame([
        {"player_id": "A", "season": 2025, "week": w,
         "carries": w * 2, "targets": w, "receptions": w}
        for w in range(1, 6)
    ])


def test_prior_weeks_excludes_the_as_of_week_itself():
    out = prior_weeks(frame(), 2025, 3)
    assert sorted(out["week"]) == [1, 2]


def test_prior_weeks_excludes_all_future_weeks():
    out = prior_weeks(frame(), 2025, 3)
    assert out["week"].max() < 3


def test_prior_weeks_includes_earlier_seasons():
    df = pd.concat([
        frame(),
        frame().assign(season=2024),
    ], ignore_index=True)
    out = prior_weeks(df, 2025, 2)
    assert set(out["season"]) == {2024, 2025}
    assert out[out["season"] == 2025]["week"].max() == 1


def test_rolling_volume_is_identical_whether_or_not_future_rows_exist():
    # THE leakage guard. Truncating the future must not change the answer.
    full = frame()
    truncated = full[full["week"] < 4]
    a = rolling_volume(full, 2025, 4)
    b = rolling_volume(truncated, 2025, 4)
    pd.testing.assert_frame_equal(
        a.reset_index(drop=True), b.reset_index(drop=True)
    )


def test_rolling_volume_averages_only_the_requested_window():
    # as_of week 5, window 3 -> weeks 2,3,4 -> carries 4,6,8 -> mean 6.0
    out = rolling_volume(frame(), 2025, 5, windows=(3,))
    row = out[out["player_id"] == "A"].iloc[0]
    assert row["carries_r3"] == pytest.approx(6.0)


def test_rolling_volume_returns_no_rows_for_a_player_with_no_history():
    out = rolling_volume(frame(), 2025, 1)
    assert out.empty


def test_shrunk_rate_pulls_a_small_sample_toward_the_prior():
    # 1 TD on 2 carries is 0.5, but two carries is nothing. With a 0.05 prior
    # and strength 50, the estimate must stay near the prior.
    assert shrunk_rate(1, 2, prior=0.05, strength=50) == pytest.approx(
        (1 + 0.05 * 50) / (2 + 50)
    )
    assert shrunk_rate(1, 2, prior=0.05, strength=50) < 0.10


def test_shrunk_rate_approaches_the_observed_rate_with_a_large_sample():
    assert shrunk_rate(100, 1000, prior=0.05, strength=50) == pytest.approx(
        (100 + 2.5) / 1050
    )


def test_shrunk_rate_returns_the_prior_with_no_observations():
    assert shrunk_rate(0, 0, prior=0.05, strength=50) == pytest.approx(0.05)
