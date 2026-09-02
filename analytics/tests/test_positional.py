"""Positional draft-timing study: the arms, the runway guard, the waiting cost.

These tests pin the properties that make this module's numbers mean what
its report claims. The three that matter most, and that a plausible-looking
rewrite would silently break:

  1. An arm's constraint ACTUALLY BINDS -- "RB-first" must draft an RB in
     rounds 1 and 2, not merely prefer one.
  2. The runway guard NEVER FIRES EARLY -- it exists to stop a roster-blind
     baseline finishing with an empty lineup slot, not to express a
     positional opinion, so if it reshaped round 1 the whole comparison
     would be measuring it rather than the arms.
  3. `value_lost_by_waiting` adds MY OWN pick back onto the next-turn board
     -- without that it scores the tautology "the player I just drafted is
     gone," which would report a huge fake cliff at every position I happen
     to draft from.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tt.league import load_config_from_dict
from tt.mock import DEFAULT_ROUNDS, simulate_draft, strategy_adp
from tt.studies.positional import (
    PLANS,
    POSITIONS,
    PositionalPlan,
    constrained_strategy,
    points_by_position,
    roster_composition,
    round_of,
    run_positional_cell,
    run_positional_study,
    starter_runway_positions,
    strategies_for,
    value_lost_by_waiting,
)

CONFIG = load_config_from_dict({
    "leagueKey": "470.l.1433971", "name": "Test League",
    "numTeams": 10, "maxTeams": 10, "draftStatus": "predraft",
    "rosterSlots": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1},
    "scoring": [
        {"statId": 4, "name": "Pass Yds", "group": "passing", "value": 0.04},
        {"statId": 5, "name": "Pass TD", "group": "passing", "value": 4},
        {"statId": 9, "name": "Rush Yds", "group": "rushing", "value": 0.1},
        {"statId": 10, "name": "Rush TD", "group": "rushing", "value": 6},
        {"statId": 11, "name": "Rec", "group": "receiving", "value": 0.5},
        {"statId": 12, "name": "Rec Yds", "group": "receiving", "value": 0.1},
        {"statId": 13, "name": "Rec TD", "group": "receiving", "value": 6},
    ],
})


def _board(per_position: int = 60) -> pd.DataFrame:
    """A synthetic board: `per_position` players at each of QB/RB/WR/TE,
    interleaved in ADP so no position is exhausted early and every round
    genuinely offers a choice between all four."""
    rows = []
    adp = 0
    for rank in range(per_position):
        for position in POSITIONS:
            adp += 1
            rows.append({
                "player_id": f"{position}{rank:02d}",
                "name": f"{position} {rank}",
                "position": position,
                "proj_points": 300.0 - rank * 7 - POSITIONS.index(position),
                "adp": float(adp),
                "stdev": 5.0,
            })
    return pd.DataFrame(rows)


def _actual(board: pd.DataFrame) -> pd.Series:
    """Actual points that deliberately DISAGREE with proj_points ordering,
    so a test can never pass by accidentally grading on projections."""
    values = board.set_index("player_id")["proj_points"] * 0.5 + 40.0
    values.name = "actual_points"
    return values


# --------------------------------------------------------------- round_of

def test_round_of_maps_pick_numbers_to_snake_rounds():
    assert round_of(1, 4) == 1
    assert round_of(4, 4) == 1
    assert round_of(5, 4) == 2
    assert round_of(150, 10) == 15


@pytest.mark.parametrize("pick,teams", [(0, 4), (-1, 4)])
def test_round_of_rejects_a_zero_indexed_pick(pick, teams):
    with pytest.raises(ValueError, match="1-indexed"):
        round_of(pick, teams)


def test_round_of_rejects_a_league_with_no_teams():
    with pytest.raises(ValueError, match="at least 1"):
        round_of(1, 0)


# ------------------------------------------------------- the runway guard

def test_runway_guard_stays_silent_while_the_roster_has_slack():
    """THE load-bearing property: with 15 rounds and 6 mandatory starters,
    an empty roster in round 1 has nine spare picks, so the guard must not
    constrain anything. If it fired here every arm would be drafting under
    the guard's opinion instead of its own."""
    assert starter_runway_positions([], CONFIG, current_round=1, rounds=15) is None


def test_runway_guard_fires_only_once_picks_left_equal_slots_owed():
    empty: list[dict] = []
    # 6 mandatory starters (QB 1, RB 2, WR 2, TE 1) in this league.
    assert starter_runway_positions(empty, CONFIG, current_round=9, rounds=15) is None
    assert starter_runway_positions(empty, CONFIG, current_round=10, rounds=15) == {
        "QB", "RB", "WR", "TE",
    }


def test_runway_guard_names_only_the_positions_still_owed():
    roster = [{"position": "RB"}, {"position": "RB"}, {"position": "WR"},
              {"position": "WR"}, {"position": "QB"}]
    assert starter_runway_positions(roster, CONFIG, current_round=15, rounds=15) == {"TE"}


def test_runway_guard_returns_none_once_every_starter_slot_is_filled():
    roster = [{"position": "RB"}, {"position": "RB"}, {"position": "WR"},
              {"position": "WR"}, {"position": "QB"}, {"position": "TE"}]
    assert starter_runway_positions(roster, CONFIG, current_round=15, rounds=15) is None


def test_runway_guard_ignores_kicker_and_defense_slots():
    """K/DEF are never projected, so they can never be drafted from this
    board -- counting them as outstanding need would burn two rounds of
    runway on slots no arm can ever fill, firing the guard four rounds
    early for every arm."""
    assert starter_runway_positions([], CONFIG, current_round=9, rounds=15) is None


# ------------------------------------------------------ constraint arming

def test_rb_first_actually_takes_running_backs_in_rounds_one_and_two():
    board = _board()
    strategy = constrained_strategy(PLANS["rb_first"], CONFIG, DEFAULT_ROUNDS)
    drafted = simulate_draft(
        board, teams=10, rounds=DEFAULT_ROUNDS, my_slot=5, strategy=strategy,
        seed=7, opponent_strategy=strategy_adp,
    )
    assert list(drafted["position"][:2]) == ["RB", "RB"]


def test_wr_first_actually_takes_receivers_in_rounds_one_and_two():
    board = _board()
    strategy = constrained_strategy(PLANS["wr_first"], CONFIG, DEFAULT_ROUNDS)
    drafted = simulate_draft(
        board, teams=10, rounds=DEFAULT_ROUNDS, my_slot=5, strategy=strategy,
        seed=7, opponent_strategy=strategy_adp,
    )
    assert list(drafted["position"][:2]) == ["WR", "WR"]


def test_zero_rb_drafts_no_running_back_before_round_five():
    board = _board()
    strategy = constrained_strategy(PLANS["zero_rb"], CONFIG, DEFAULT_ROUNDS)
    drafted = simulate_draft(
        board, teams=10, rounds=DEFAULT_ROUNDS, my_slot=5, strategy=strategy,
        seed=11, opponent_strategy=strategy_adp,
    )
    assert "RB" not in set(drafted["position"][:4])
    assert "RB" in set(drafted["position"])


def test_late_qb_drafts_no_quarterback_before_round_eight():
    board = _board()
    strategy = constrained_strategy(PLANS["late_qb"], CONFIG, DEFAULT_ROUNDS)
    drafted = simulate_draft(
        board, teams=10, rounds=DEFAULT_ROUNDS, my_slot=5, strategy=strategy,
        seed=3, opponent_strategy=strategy_adp,
    )
    assert "QB" not in set(drafted["position"][:7])


def test_early_qb_has_a_quarterback_by_the_end_of_round_three():
    board = _board()
    strategy = constrained_strategy(PLANS["early_qb"], CONFIG, DEFAULT_ROUNDS)
    drafted = simulate_draft(
        board, teams=10, rounds=DEFAULT_ROUNDS, my_slot=5, strategy=strategy,
        seed=3, opponent_strategy=strategy_adp,
    )
    assert "QB" in set(drafted["position"][:3])


def test_early_te_has_a_tight_end_by_the_end_of_round_three():
    board = _board()
    strategy = constrained_strategy(PLANS["early_te"], CONFIG, DEFAULT_ROUNDS)
    drafted = simulate_draft(
        board, teams=10, rounds=DEFAULT_ROUNDS, my_slot=5, strategy=strategy,
        seed=3, opponent_strategy=strategy_adp,
    )
    assert "TE" in set(drafted["position"][:3])


def test_bpa_is_exactly_unconstrained_adp_while_the_runway_guard_sleeps():
    """The control arm must be the market baseline, not a fifth strategy.
    Before the guard can fire (rounds 1-9 here) `bpa` and bare
    `strategy_adp` must make identical picks."""
    board = _board()
    bpa = constrained_strategy(PLANS["bpa"], CONFIG, DEFAULT_ROUNDS)
    guarded = simulate_draft(board, 10, 9, 5, bpa, seed=21, opponent_strategy=strategy_adp)
    bare = simulate_draft(board, 10, 9, 5, strategy_adp, seed=21, opponent_strategy=strategy_adp)
    assert list(guarded["player_id"]) == list(bare["player_id"])


def test_a_constraint_matching_nobody_left_is_dropped_rather_than_raising():
    """A board with no tight end on it at all must not abort the draft --
    "there were no tight ends left" is a fact about the board."""
    board = _board()
    board = board[board["position"] != "TE"].reset_index(drop=True)
    strategy = constrained_strategy(PLANS["early_te"], CONFIG, DEFAULT_ROUNDS)
    drafted = simulate_draft(
        board, teams=10, rounds=5, my_slot=5, strategy=strategy,
        seed=3, opponent_strategy=strategy_adp,
    )
    assert len(drafted) == 5
    assert "TE" not in set(drafted["position"])


def test_a_deadline_overrides_an_earlier_ban_on_the_same_position():
    plan = PositionalPlan(forbid_before={"TE": 9}, require_by={"TE": 3})
    allowed = plan.allowed(current_round=3, roster=[], available=set(POSITIONS))
    assert allowed == {"TE"}


def test_a_satisfied_deadline_stops_constraining_later_rounds():
    plan = PositionalPlan(require_by={"QB": 3})
    assert plan.allowed(3, [{"position": "QB"}], set(POSITIONS)) is None
    assert plan.allowed(3, [{"position": "RB"}], set(POSITIONS)) == {"QB"}


def test_an_only_in_rounds_window_does_not_leak_past_its_round():
    plan = PLANS["rb_first"]
    assert plan.allowed(2, [], set(POSITIONS)) == {"RB"}
    assert plan.allowed(3, [], set(POSITIONS)) is None


def test_forbid_before_bans_up_to_but_not_including_its_own_round():
    """The boundary, pinned exactly. `forbid_before={"RB": 5}` means "no RB
    until round 5" -- banned through round 4, free from round 5 on. An
    off-by-one here silently turns Zero-RB into a four-round rule that
    matches its name in the report and not in the simulation."""
    plan = PLANS["zero_rb"]
    assert plan.allowed(4, [], set(POSITIONS)) == {"QB", "WR", "TE"}
    assert plan.allowed(5, [], set(POSITIONS)) is None


def test_late_arms_ban_their_position_through_round_seven_exactly():
    assert PLANS["late_qb"].allowed(7, [], set(POSITIONS)) == {"RB", "WR", "TE"}
    assert PLANS["late_qb"].allowed(8, [], set(POSITIONS)) is None
    assert PLANS["late_te"].allowed(7, [], set(POSITIONS)) == {"QB", "RB", "WR"}
    assert PLANS["late_te"].allowed(8, [], set(POSITIONS)) is None


def test_require_by_does_not_bind_before_its_own_round():
    """early-QB means "a QB by the END of round 3", so rounds 1 and 2 stay
    free -- an arm that forced a QB in round 1 would be a different, much
    more aggressive strategy than the one the report names."""
    plan = PLANS["early_qb"]
    assert plan.allowed(1, [], set(POSITIONS)) is None
    assert plan.allowed(2, [], set(POSITIONS)) is None
    assert plan.allowed(3, [], set(POSITIONS)) == {"QB"}
    # And it stays forced past the deadline until it is actually satisfied.
    assert plan.allowed(4, [], set(POSITIONS)) == {"QB"}


def test_every_named_arm_is_built_and_is_callable():
    strategies = strategies_for(CONFIG, DEFAULT_ROUNDS)
    assert set(strategies) == set(PLANS)
    board = _board()
    for strategy in strategies.values():
        chosen = strategy(board, [], 1, 20, 10)
        assert chosen["player_id"] in set(board["player_id"])


# ------------------------------------------------------ grading is honest

def test_run_positional_cell_grades_on_actual_points_not_projections():
    """Swap the actual-points series for a different one and the scores
    must move. If they don't, the cell is grading on `proj_points` -- the
    circular metric this whole line of work exists to avoid."""
    board = _board()
    real = run_positional_cell(
        board, CONFIG, 2024, 10, 5, trials=3, seed=1,
        actual_points=_actual(board), rounds=6,
    )
    flipped = run_positional_cell(
        board, CONFIG, 2024, 10, 5, trials=3, seed=1,
        actual_points=_actual(board) * 2.0, rounds=6,
    )
    assert not np.allclose(real["mean_score"], flipped["mean_score"])


def test_a_drafted_player_with_no_actual_points_scores_zero_not_nan():
    board = _board()
    empty = pd.Series(dtype=float, name="actual_points")
    result = run_positional_cell(
        board, CONFIG, 2024, 10, 5, trials=2, seed=1, actual_points=empty, rounds=4,
    )
    assert (result["mean_score"] == 0.0).all()


def test_run_positional_cell_reports_a_confidence_interval_per_arm():
    board = _board()
    result = run_positional_cell(
        board, CONFIG, 2024, 10, 5, trials=5, seed=1,
        actual_points=_actual(board), rounds=6,
    )
    assert set(result["strategy"]) == set(PLANS)
    assert (result["ci95_low"] <= result["mean_score"]).all()
    assert (result["mean_score"] <= result["ci95_high"]).all()
    assert (result["season"] == 2024).all() and (result["teams"] == 10).all()


def test_the_study_is_reproducible_at_a_fixed_seed():
    board = _board()
    actual = _actual(board)
    boards = {(2024, 10): board}
    first = run_positional_study(boards, CONFIG, {2024: actual}, trials=4, seed=99, rounds=6)
    second = run_positional_study(boards, CONFIG, {2024: actual}, trials=4, seed=99, rounds=6)
    pd.testing.assert_frame_equal(first, second)


def test_run_positional_study_covers_every_board_cell_it_is_given():
    board = _board()
    actual = _actual(board)
    boards = {(2024, 4): board, (2024, 10): board}
    seen = []
    result = run_positional_study(
        boards, CONFIG, {2024: actual}, trials=2, seed=5, rounds=4,
        on_cell=lambda season, teams, *_: seen.append((season, teams)),
    )
    assert seen == [(2024, 4), (2024, 10)]
    assert set(zip(result["season"], result["teams"])) == {(2024, 4), (2024, 10)}


# ------------------------------------------------- composition diagnostic

def test_roster_composition_reports_no_empty_slots_under_the_runway_guard():
    """The guard's entire job. Every arm must finish a full-length draft
    with all six mandatory starters filled."""
    board = _board()
    composition = roster_composition(
        board, CONFIG, teams=10, my_slot=5, seed=4, rounds=DEFAULT_ROUNDS, trials=3,
    )
    assert set(composition["strategy"]) == set(PLANS)
    assert (composition["empty_slots"] == 0.0).all()


# --------------------------------------------------- value lost by waiting

def test_value_lost_by_waiting_adds_my_own_pick_back_before_looking_ahead():
    """The counterfactual is "if I DON'T take him, is he still there next
    turn" -- a question about the OTHER teams. Scoring my own pick as gone
    would report a fake cliff at whatever position I happened to draft.

    Hand-computed, with the ADP noise switched off so the draft order is
    exact. Two teams, two rounds, my_slot 0: I take RB0 (best ADP) at pick
    1, the opponent takes RB1 and RB2 at picks 2 and 3, and I pick again at
    4. RB0 is MINE, so on the "did waiting cost me anything" board he is
    still there -- the best RB available to me is RB0 either way and the
    waiting cost at RB is exactly 0. Drop the add-back and the best RB left
    becomes RB3, reporting a 30-point cliff that does not exist.
    """
    board = pd.DataFrame([
        {"player_id": "RB0", "name": "RB0", "position": "RB", "proj_points": 100.0, "adp": 1.0},
        {"player_id": "RB1", "name": "RB1", "position": "RB", "proj_points": 90.0, "adp": 2.0},
        {"player_id": "RB2", "name": "RB2", "position": "RB", "proj_points": 80.0, "adp": 3.0},
        {"player_id": "RB3", "name": "RB3", "position": "RB", "proj_points": 70.0, "adp": 4.0},
        {"player_id": "WR0", "name": "WR0", "position": "WR", "proj_points": 60.0, "adp": 5.0},
        {"player_id": "WR1", "name": "WR1", "position": "WR", "proj_points": 50.0, "adp": 6.0},
        {"player_id": "QB0", "name": "QB0", "position": "QB", "proj_points": 40.0, "adp": 7.0},
        {"player_id": "QB1", "name": "QB1", "position": "QB", "proj_points": 30.0, "adp": 8.0},
        {"player_id": "TE0", "name": "TE0", "position": "TE", "proj_points": 20.0, "adp": 9.0},
        {"player_id": "TE1", "name": "TE1", "position": "TE", "proj_points": 10.0, "adp": 10.0},
    ])
    actual = board.set_index("player_id")["proj_points"]
    frame = value_lost_by_waiting(
        board, CONFIG, teams=2, my_slot=0, seed=1, actual_points=actual,
        rounds=2, trials=1, adp_noise=0.0,
    )
    rb = frame[(frame["round"] == 1) & (frame["position"] == "RB")]
    assert len(rb) == 1
    assert rb["value_now"].iloc[0] == 100.0
    assert rb["value_next"].iloc[0] == 100.0
    assert rb["value_lost"].iloc[0] == 0.0


def test_value_lost_by_waiting_charges_a_position_the_opponents_actually_took():
    """The other half of the same hand-computed draft: with three teams the
    opponents take RB1 and RB2 AND WR0 before my next turn, so waiting on
    WR really does cost the gap from WR0 to WR1."""
    board = pd.DataFrame([
        {"player_id": "RB0", "name": "RB0", "position": "RB", "proj_points": 100.0, "adp": 1.0},
        {"player_id": "RB1", "name": "RB1", "position": "RB", "proj_points": 90.0, "adp": 2.0},
        {"player_id": "RB2", "name": "RB2", "position": "RB", "proj_points": 80.0, "adp": 3.0},
        {"player_id": "WR0", "name": "WR0", "position": "WR", "proj_points": 60.0, "adp": 4.0},
        {"player_id": "WR1", "name": "WR1", "position": "WR", "proj_points": 50.0, "adp": 5.0},
        {"player_id": "WR2", "name": "WR2", "position": "WR", "proj_points": 45.0, "adp": 6.0},
        {"player_id": "QB0", "name": "QB0", "position": "QB", "proj_points": 40.0, "adp": 7.0},
        {"player_id": "TE0", "name": "TE0", "position": "TE", "proj_points": 20.0, "adp": 8.0},
    ])
    actual = board.set_index("player_id")["proj_points"]
    frame = value_lost_by_waiting(
        board, CONFIG, teams=3, my_slot=0, seed=1, actual_points=actual,
        rounds=2, trials=1, adp_noise=0.0,
    ).set_index("position")
    # Pick 1 is mine (RB0); the opponents take RB1, RB2 at 2-3 and then
    # WR0, WR1 at 4-5 (round 2 runs in reverse); I pick again at 6. RB0 is
    # mine, so RB costs nothing to wait on; the best WR left has fallen
    # from WR0's 60 to WR2's 45.
    assert frame.loc["RB", "value_lost"] == 0.0
    assert frame.loc["WR", "value_now"] == 60.0
    assert frame.loc["WR", "value_next"] == 45.0
    assert frame.loc["WR", "value_lost"] == 15.0


def test_value_lost_by_waiting_is_zero_when_nobody_else_drafts_that_position():
    """A one-team "draft" has no opponents at all, so nothing can be taken
    away between my picks and every position's waiting cost is exactly 0."""
    board = _board()
    frame = value_lost_by_waiting(
        board, CONFIG, teams=1, my_slot=0, seed=2, actual_points=_actual(board),
        rounds=5, trials=1,
    )
    assert (frame["value_lost"] == 0.0).all()


def test_value_lost_by_waiting_uses_actual_points_not_projections():
    board = _board()
    base = value_lost_by_waiting(
        board, CONFIG, teams=10, my_slot=5, seed=8,
        actual_points=_actual(board), rounds=5, trials=2,
    )
    doubled = value_lost_by_waiting(
        board, CONFIG, teams=10, my_slot=5, seed=8,
        actual_points=_actual(board) * 2.0, rounds=5, trials=2,
    )
    assert np.allclose(doubled["value_lost"], base["value_lost"] * 2.0)


def test_value_lost_by_waiting_covers_every_round_but_the_last():
    board = _board()
    frame = value_lost_by_waiting(
        board, CONFIG, teams=10, my_slot=5, seed=8,
        actual_points=_actual(board), rounds=6, trials=1,
    )
    # No "next pick" exists after the final round, so it is not reported.
    assert sorted(frame["round"].unique()) == [1, 2, 3, 4, 5]
    assert set(frame["position"]) == set(POSITIONS)


# ---------------------------------------------------- points_by_position

def test_points_by_position_best_vor_is_exactly_best_minus_replacement():
    board = _board()
    table = points_by_position(board, CONFIG, teams=10)
    assert np.allclose(
        table["best_vor"], table["best_points"] - table["replacement_points"],
    )


def test_points_by_position_uses_the_same_replacement_rank_add_vor_did():
    """RB/WR carry a third of the flex slot each, so their replacement rank
    is round(2.333 * teams) = 23, NOT 2 * teams. A table that quietly used
    the rounded per-team starter count would report a different, wrong
    replacement player than the `vor` column beside it."""
    table = points_by_position(_board(), CONFIG, teams=10).set_index("position")
    assert table.loc["RB", "replacement_rank"] == 23
    assert table.loc["WR", "replacement_rank"] == 23
    assert table.loc["TE", "replacement_rank"] == 13
    assert table.loc["QB", "replacement_rank"] == 10


def test_points_by_position_adds_actual_columns_only_when_asked():
    board = _board()
    without = points_by_position(board, CONFIG, teams=10)
    assert "actual_best_vor" not in without.columns
    with_actual = points_by_position(board, CONFIG, teams=10, actual_points=_actual(board))
    assert np.allclose(
        with_actual["actual_best_vor"],
        with_actual["actual_best_points"] - with_actual["actual_replacement_points"],
    )
