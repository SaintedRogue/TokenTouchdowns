"""The pick recommender: which available player costs the most to defer.

The naive strategy -- take the highest-VOR player left -- is wrong whenever
that player would still be there at your NEXT pick anyway. What actually
matters is the value you would LOSE by waiting: `vor * P(gone before my next
pick)`. A superb player who is certain to survive scores low (you can safely
wait on him); a merely-good player who is certain to vanish scores high (now
or never). These tests pin that ranking rule, the roster-need discount that
keeps the recommender from stacking a 5th RB on a team that already has 4,
and the hard dependency on `survival.add_survival` having already run.

`recommend` deliberately takes `p_gone_by_next` as an INPUT column on the
board rather than computing it -- see `tt.draft` module docstring for why
(Task 8 needs to swap conditional vs. unconditional survival under the same
recommender without touching this module).
"""
import pandas as pd
import pytest

from tt.draft import recommend, roster_need
from tt.league import load_config_from_dict

# A 2-way "W/R" flex (not the real league's 3-way "W/R/T") so starters_per_team
# lands on clean numbers, same rationale as test_vor.py's CONFIG.
CONFIG = {
    "leagueKey": "470.l.1433971", "name": "Test League",
    "numTeams": 10, "maxTeams": 10, "draftStatus": "predraft",
    "rosterSlots": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "W/R": 1, "K": 1, "DEF": 1},
    "scoring": [
        {"statId": 4, "name": "Pass Yds", "group": "passing", "value": 0.04},
        {"statId": 5, "name": "Pass TD", "group": "passing", "value": 4},
    ],
}
CONFIG_OBJ = load_config_from_dict(CONFIG)


def test_recommends_the_player_you_would_lose_not_the_best_one():
    # THE core rule. Two players of near-equal VOR: one certain to survive to
    # your next pick (p_gone_by_next ~ 0), one certain to be gone (~1). Take
    # the one you would lose, even though "safe" has the higher raw VOR.
    board = pd.DataFrame([
        {"player_id": "safe", "position": "RB", "vor": 50.0, "p_gone_by_next": 0.01},
        {"player_id": "scarce", "position": "WR", "vor": 48.0, "p_gone_by_next": 0.98},
    ])
    out = recommend(board, pick=20, next_pick=30, roster=[], config=CONFIG_OBJ, teams=10, rounds_remaining=15)
    assert out.iloc[0]["player_id"] == "scarce"


def test_a_player_certain_to_survive_has_near_zero_expected_loss():
    board = pd.DataFrame([
        {"player_id": "safe", "position": "RB", "vor": 50.0, "p_gone_by_next": 0.001},
    ])
    out = recommend(board, pick=20, next_pick=30, roster=[], config=CONFIG_OBJ, teams=10, rounds_remaining=15)
    assert out.iloc[0]["expected_loss"] < 1.0


def test_a_player_already_on_your_own_roster_is_never_recommended_again():
    # Defensive filtering: the board is documented (survival.py) to contain
    # only undrafted players, so this should never trigger in a correctly
    # wired pipeline -- but a recommender that could re-suggest a player you
    # already rostered would be actively dangerous if that invariant ever
    # slipped, so it is enforced here too.
    board = pd.DataFrame([
        {"player_id": "already_mine", "position": "RB", "vor": 99.0, "p_gone_by_next": 0.9},
        {"player_id": "available", "position": "RB", "vor": 10.0, "p_gone_by_next": 0.9},
    ])
    roster = [{"player_id": "already_mine", "position": "RB"}]
    out = recommend(board, pick=20, next_pick=30, roster=roster, config=CONFIG_OBJ, teams=10, rounds_remaining=15)
    assert "already_mine" not in set(out["player_id"])


def test_a_filled_position_is_deprioritised():
    # RB starters_per_team here is 2 + (1/2 flex) = 2.5 -> round to 2. Three
    # RBs already rostered is past that, so the position is "filled"; TE
    # (1 + 1/2 flex = 1.5 -> round to 2, none rostered) still has open need.
    # Equal VOR and equal p_gone_by_next -- need is the only thing that can
    # break the tie, and it must favour the position still needed.
    board = pd.DataFrame([
        {"player_id": "rb", "position": "RB", "vor": 30.0, "p_gone_by_next": 0.5},
        {"player_id": "te", "position": "TE", "vor": 30.0, "p_gone_by_next": 0.5},
    ])
    roster = [{"player_id": f"r{i}", "position": "RB"} for i in range(3)]
    out = recommend(board, pick=20, next_pick=30, roster=roster, config=CONFIG_OBJ, teams=10, rounds_remaining=15)
    assert out.iloc[0]["player_id"] == "te"


def test_missing_p_gone_by_next_raises_a_clear_error_naming_add_survival():
    # CRITICAL: recommend must never silently compute survival itself -- see
    # module docstring. A board that skipped `add_survival` is a caller bug,
    # not something to paper over with an implicit default.
    board = pd.DataFrame([
        {"player_id": "x", "position": "RB", "vor": 10.0},
    ])
    with pytest.raises(ValueError, match="add_survival"):
        recommend(board, pick=20, next_pick=30, roster=[], config=CONFIG_OBJ, teams=10, rounds_remaining=15)


def test_rows_with_no_vor_are_excluded_from_recommendations():
    # add_vor leaves `vor` as NaN for positions the league doesn't start
    # (see tt.vor docstring). NaN * p_gone_by_next is NaN and cannot be
    # meaningfully ranked, so those rows must not surface as "recommended."
    board = pd.DataFrame([
        {"player_id": "unscored", "position": "DT", "vor": float("nan"), "p_gone_by_next": 0.9},
        {"player_id": "scored", "position": "RB", "vor": 5.0, "p_gone_by_next": 0.9},
    ])
    out = recommend(board, pick=20, next_pick=30, roster=[], config=CONFIG_OBJ, teams=10, rounds_remaining=15)
    assert "unscored" not in set(out["player_id"])


def test_n_limits_the_number_of_rows_returned():
    board = pd.DataFrame([
        {"player_id": f"p{i}", "position": "RB", "vor": float(i), "p_gone_by_next": 0.5}
        for i in range(10)
    ])
    out = recommend(board, pick=20, next_pick=30, roster=[], config=CONFIG_OBJ, teams=10, rounds_remaining=15, n=3)
    assert len(out) == 3


def test_recommend_output_includes_the_reasoning_columns():
    board = pd.DataFrame([
        {"player_id": "x", "position": "RB", "vor": 10.0, "p_gone_by_next": 0.5},
    ])
    out = recommend(board, pick=20, next_pick=30, roster=[], config=CONFIG_OBJ, teams=10, rounds_remaining=15)
    for column in ("player_id", "position", "vor", "p_gone_by_next", "expected_loss"):
        assert column in out.columns


def test_roster_need_is_zero_once_starters_per_team_is_met():
    roster = [{"player_id": f"r{i}", "position": "RB"} for i in range(3)]
    need = roster_need(roster, CONFIG_OBJ)
    # starters_per_team RB = 2 + 1/2 (W/R flex) = 2.5 -> round(2.5) == 2
    assert need["RB"] == 0


def test_roster_need_is_positive_for_an_unfilled_position():
    need = roster_need([], CONFIG_OBJ)
    assert need["TE"] > 0


def test_roster_need_never_goes_negative():
    # Rostering more than the starter target (e.g. handcuffs, a 4th RB)
    # must not produce a negative "need" that could invert the discount.
    roster = [{"player_id": f"r{i}", "position": "RB"} for i in range(10)]
    need = roster_need(roster, CONFIG_OBJ)
    assert need["RB"] == 0


# --- F11: an unfilled mandatory slot must exert POSITIVE pressure, growing
# as the draft runs out of rounds to fill it -- FILLED_POSITION_DISCOUNT
# alone only ever discounts a position already met; nothing previously
# pushed a roster back toward one it still needed. ---


def test_need_urgency_is_flat_for_a_position_with_no_unmet_need():
    from tt.draft import _need_urgency

    assert _need_urgency("RB", {"RB": 0}, rounds_remaining=1) == 1.0


def test_need_urgency_grows_as_rounds_run_out():
    from tt.draft import _need_urgency

    # need=1 (a single unmet mandatory slot -- e.g. a starting TE) at two
    # very different points in a draft: 12 rounds still to go (the brief's
    # own "non-issue" example) versus the literal last round (an emergency).
    far = _need_urgency("TE", {"TE": 1}, rounds_remaining=12)
    close = _need_urgency("TE", {"TE": 1}, rounds_remaining=1)
    assert 1.0 < far < 2.0
    assert close > 8.0


def test_need_urgency_treats_running_out_of_runway_as_no_worse_than_zero_slack():
    from tt.draft import _need_urgency

    # Two mandatory slots (need=2) but only one round left: already
    # impossible to fully fill, same as needing exactly the rounds left --
    # slack floors at 0 rather than going negative and somehow exceeding the
    # already-maximal urgency multiplier.
    exactly_enough = _need_urgency("RB", {"RB": 1}, rounds_remaining=1)
    already_short = _need_urgency("RB", {"RB": 2}, rounds_remaining=1)
    assert exactly_enough == already_short


def test_an_empty_mandatory_slot_is_prioritised_as_rounds_run_out():
    # TE (need=1, never rostered) against an already-filled RB (3 rostered
    # against a target of 2) of clearly higher raw VOR. With only 1 round
    # left, F11's need-urgency boost must overcome FILLED_POSITION_DISCOUNT
    # and the raw VOR gap both -- this roster cannot field a lineup without
    # a TE, and after this pick there is no more draft left to get one.
    board = pd.DataFrame([
        {"player_id": "rb4", "position": "RB", "vor": 40.0, "p_gone_by_next": 0.3},
        {"player_id": "te1", "position": "TE", "vor": 15.0, "p_gone_by_next": 0.3},
    ])
    roster = [{"player_id": f"r{i}", "position": "RB"} for i in range(3)]
    out = recommend(
        board, pick=140, next_pick=150, roster=roster, config=CONFIG_OBJ,
        teams=10, rounds_remaining=1,
    )
    assert out.iloc[0]["player_id"] == "te1"
