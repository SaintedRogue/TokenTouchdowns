import numpy as np
import pandas as pd
import pytest
from tt.studies.dvp import split_half_reliability


def synthetic(consistent: bool):
    """Defences whose points-allowed either persists across halves or does not."""
    rng = np.random.default_rng(0)
    rows = []
    for team_index in range(32):
        strength = team_index / 32.0
        for week in range(1, 19):
            first_half = week <= 9
            level = strength if (consistent or first_half) else (1.0 - strength)
            rows.append({
                "opponent_team": f"T{team_index:02d}",
                "position": "RB",
                "week": week,
                "points": level * 10 + rng.normal(0, 0.01),
            })
    return pd.DataFrame(rows)


def test_split_half_reliability_is_high_for_a_consistent_defence():
    out = split_half_reliability(synthetic(consistent=True), positions=("RB",))
    assert out.set_index("position").loc["RB", "r"] > 0.95


def test_split_half_reliability_is_negative_when_halves_invert():
    out = split_half_reliability(synthetic(consistent=False), positions=("RB",))
    assert out.set_index("position").loc["RB", "r"] < -0.95


def test_split_half_reliability_reports_the_team_count():
    out = split_half_reliability(synthetic(consistent=True), positions=("RB",))
    assert out.set_index("position").loc["RB", "teams"] == 32
