"""ADP survival: will this player still be on the board at my next pick?

FFC's `adp`/`stdev`/`high`/`low` are exactly the input a survival model
needs -- `stdev` is what turns a single mean draft position into a real
distribution over draft slots. These tests pin `p_available`'s normal-CDF
behaviour (including the zero-stdev step-function edge case FFC's own data
produces) and `add_survival`'s job of decorating a board with the two
columns the recommender multiplies by VOR.
"""
import pandas as pd
import pytest

from tt.survival import add_survival, p_available


def test_a_player_drafted_far_earlier_is_almost_never_available():
    assert p_available(adp=5.0, stdev=2.0, pick=40) < 0.01


def test_a_player_drafted_far_later_is_almost_always_available():
    assert p_available(adp=120.0, stdev=10.0, pick=40) > 0.99


def test_probability_is_one_half_at_the_adp_itself():
    assert p_available(adp=40.0, stdev=8.0, pick=40) == pytest.approx(0.5, abs=0.01)


def test_availability_falls_monotonically_as_the_pick_gets_later():
    ps = [p_available(adp=40.0, stdev=8.0, pick=k) for k in (10, 30, 50, 70)]
    assert ps == sorted(ps, reverse=True)


def test_a_zero_stdev_player_is_a_step_function():
    # FFC occasionally reports stdev 0 for rarely-drafted players. It must not
    # divide by zero.
    assert p_available(adp=40.0, stdev=0.0, pick=39) == 1.0
    assert p_available(adp=40.0, stdev=0.0, pick=41) == 0.0


def test_a_missing_stdev_is_treated_the_same_as_zero():
    # NaN fails every `<= 0` comparison silently in Python, so this is a
    # distinct guard from the explicit-zero case above -- a `pd.isna` check,
    # not just `stdev <= 0`.
    assert p_available(adp=40.0, stdev=float("nan"), pick=39) == 1.0
    assert p_available(adp=40.0, stdev=float("nan"), pick=41) == 0.0


def test_a_player_with_no_adp_at_all_is_treated_as_certainly_available():
    # Undrafted in FFC's whole sample: nobody is taking them, so the honest
    # default is 1.0, not NaN propagating into a downstream VOR multiply.
    assert p_available(adp=float("nan"), stdev=5.0, pick=1) == 1.0
    assert p_available(adp=None, stdev=5.0, pick=250) == 1.0


def test_p_available_is_high_before_the_earliest_a_player_has_ever_gone():
    # Sanity check against FFC's own high/low semantics (design doc §5): a
    # player whose EARLIEST-ever draft slot is pick 3 should still be
    # considered very likely available at pick 3 itself -- going that early
    # is the tail, not the typical case, for a player with adp=10.
    assert p_available(adp=10.0, stdev=3.0, pick=3) > 0.9


def test_p_available_is_low_after_the_latest_a_player_has_ever_gone():
    # Mirror check: a player's LATEST-ever draft slot (low=20 for adp=10)
    # should be a near-certain "gone" by then.
    assert p_available(adp=10.0, stdev=3.0, pick=20) < 0.05


def _board():
    return pd.DataFrame([
        {"player_id": "scarce", "name": "Scarce Guy", "position": "WR", "adp": 21.0, "stdev": 3.0},
        {"player_id": "safe", "name": "Safe Guy", "position": "RB", "adp": 200.0, "stdev": 5.0},
        {"player_id": "stepfn", "name": "Step Guy", "position": "TE", "adp": 40.0, "stdev": 0.0},
        {"player_id": "noadp", "name": "No ADP Guy", "position": "QB", "adp": float("nan"), "stdev": float("nan")},
    ])


def test_add_survival_adds_both_probability_columns():
    out = add_survival(_board(), pick=20, next_pick=30)
    assert "p_available_next" in out.columns
    assert "p_gone_by_next" in out.columns


def test_add_survival_p_gone_is_one_minus_p_available():
    out = add_survival(_board(), pick=20, next_pick=30)
    pd.testing.assert_series_equal(
        out["p_gone_by_next"], 1.0 - out["p_available_next"], check_names=False,
    )


def test_add_survival_marks_a_scarce_player_as_likely_gone_by_next_pick():
    # adp=21, stdev=3: essentially always gone before pick 30.
    out = add_survival(_board(), pick=20, next_pick=30).set_index("player_id")
    assert out.loc["scarce", "p_gone_by_next"] > 0.9


def test_add_survival_marks_a_deep_adp_player_as_safe_until_next_pick():
    # adp=200: certain to survive from pick 20 to pick 30.
    out = add_survival(_board(), pick=20, next_pick=30).set_index("player_id")
    assert out.loc["safe", "p_gone_by_next"] < 0.01


def test_add_survival_handles_the_zero_stdev_step_function_in_a_board():
    # adp=40, stdev=0: certainly gone by next_pick=41.
    out = add_survival(_board(), pick=30, next_pick=41).set_index("player_id")
    assert out.loc["stepfn", "p_available_next"] == 0.0


def test_add_survival_treats_a_no_adp_player_as_certainly_available():
    out = add_survival(_board(), pick=20, next_pick=30).set_index("player_id")
    assert out.loc["noadp", "p_available_next"] == 1.0
    assert out.loc["noadp", "p_gone_by_next"] == 0.0


def test_add_survival_preserves_the_rest_of_the_board_untouched():
    out = add_survival(_board(), pick=20, next_pick=30)
    assert out["name"].tolist() == ["Scarce Guy", "Safe Guy", "Step Guy", "No ADP Guy"]
    assert out["position"].tolist() == ["WR", "RB", "TE", "QB"]


def test_add_survival_does_not_mutate_the_input_board():
    board = _board()
    original_columns = list(board.columns)
    add_survival(board, pick=20, next_pick=30)
    assert list(board.columns) == original_columns


def test_add_survival_rejects_a_next_pick_that_is_not_after_pick():
    # "Will they last from my pick to my NEXT one" is only meaningful
    # looking forward -- a next_pick <= pick is a caller bug, not a
    # probability question, so this fails loudly rather than returning a
    # nonsensical number.
    with pytest.raises(ValueError):
        add_survival(_board(), pick=20, next_pick=20)
    with pytest.raises(ValueError):
        add_survival(_board(), pick=20, next_pick=10)
