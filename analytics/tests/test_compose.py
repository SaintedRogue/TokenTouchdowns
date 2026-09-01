import numpy as np
import pytest
from tt.models.compose import simulate_points, summarise


def test_simulate_points_returns_the_requested_sample_count():
    s = simulate_points(volume=10.0, eff_rate=4.0, td_rate=0.05, n=500, seed=1)
    assert len(s) == 500


def test_simulate_points_is_deterministic_for_a_fixed_seed():
    a = simulate_points(volume=10.0, eff_rate=4.0, td_rate=0.05, n=200, seed=7)
    b = simulate_points(volume=10.0, eff_rate=4.0, td_rate=0.05, n=200, seed=7)
    assert np.array_equal(a, b)


def test_higher_volume_raises_the_mean():
    low = simulate_points(volume=5.0, eff_rate=4.0, td_rate=0.05, n=4000, seed=3).mean()
    high = simulate_points(volume=15.0, eff_rate=4.0, td_rate=0.05, n=4000, seed=3).mean()
    assert high > low


def test_a_higher_td_rate_widens_the_distribution():
    # Touchdowns are the lumpy component: 6 points arriving at random is the
    # dominant source of week-to-week variance.
    calm = simulate_points(volume=10.0, eff_rate=4.0, td_rate=0.01, n=4000, seed=5)
    spiky = simulate_points(volume=10.0, eff_rate=4.0, td_rate=0.30, n=4000, seed=5)
    assert spiky.std() > calm.std()


def test_summarise_reports_ordered_quantiles():
    s = simulate_points(volume=10.0, eff_rate=4.0, td_rate=0.05, n=4000, seed=11)
    out = summarise(s)
    assert out["p10"] <= out["p50"] <= out["p90"]
    assert set(out) == {"mean", "sd", "p10", "p50", "p90"}


def test_summarise_of_a_constant_series_has_zero_spread():
    out = summarise(np.full(100, 7.0))
    assert out["mean"] == pytest.approx(7.0)
    assert out["sd"] == pytest.approx(0.0)
    assert out["p10"] == pytest.approx(out["p90"])
