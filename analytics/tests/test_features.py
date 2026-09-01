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
    # The earlier season must be included IN FULL, not truncated by week --
    # a global `week < as_of_week` filter that ignores season entirely would
    # also satisfy the two assertions above while wrongly cutting 2024 down
    # to its week-1 row instead of keeping all five weeks.
    assert sorted(out[out["season"] == 2024]["week"]) == [1, 2, 3, 4, 5]


def test_prior_weeks_excludes_future_seasons_entirely():
    # The most realistic leak vector once several seasons sit in one frame:
    # a mutation from `season < as_of` to `season != as_of` would expose 2026
    # while passing every single-season test.
    df = pd.concat([frame(), frame().assign(season=2024), frame().assign(season=2026)],
                   ignore_index=True)
    out = prior_weeks(df, 2025, 3)
    assert set(out["season"]) == {2024, 2025}
    assert 2026 not in set(out["season"])


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


def test_rolling_volume_orders_across_seasons_not_by_week_alone():
    # 2024 weeks 1-5 at low volume, 2025 weeks 1-2 at high volume. Sorting by
    # week alone would pull late-2024 rows ahead of early-2025 ones and average
    # the wrong set.
    old = pd.DataFrame([{"player_id": "A", "season": 2024, "week": w,
                         "carries": 1, "targets": 1, "receptions": 1} for w in range(1, 6)])
    new = pd.DataFrame([{"player_id": "A", "season": 2025, "week": w,
                         "carries": 100, "targets": 100, "receptions": 100} for w in (1, 2)])
    out = rolling_volume(pd.concat([old, new], ignore_index=True), 2025, 3, windows=(3,))
    # last 3 rows chronologically = 2024wk5(1), 2025wk1(100), 2025wk2(100) -> 67.0
    assert out.set_index("player_id").loc["A", "carries_r3"] == pytest.approx(67.0, abs=0.5)


def test_rolling_volume_returns_no_rows_for_a_player_with_no_history():
    out = rolling_volume(frame(), 2025, 1)
    assert out.empty


def test_rolling_volume_empty_history_has_the_same_columns_as_populated_output():
    # Downstream code doing out["carries_r3"] must not KeyError just because
    # it happened to land on the first predictable week of the earliest
    # season -- i.e. iteration 1 of any walk-forward sweep.
    populated = rolling_volume(frame(), 2025, 5, windows=(3, 8))
    empty = rolling_volume(frame(), 2025, 1, windows=(3, 8))
    assert list(empty.columns) == list(populated.columns)


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


def test_shrunk_rate_returns_the_prior_when_denominator_and_strength_are_both_zero():
    # denominator=0 and strength=0 previously divided by zero. With no
    # observations and no prior weight, the prior is the only defensible
    # estimate to fall back to.
    assert shrunk_rate(0, 0, prior=0.05, strength=0) == pytest.approx(0.05)
