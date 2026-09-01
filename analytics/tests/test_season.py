"""The season / championship simulator (`tt.season`).

WHAT THESE TESTS PIN, AND WHY THEY ARE MOSTLY EXACT NUMBERS. This league
puts EVERY team in the playoffs (num_playoff_teams == num_teams == 4), so
"playoff probability" is identically 1.0 and carries no information; the
only objective worth simulating is `P(championship)`. That makes the
correctness bar unusually concrete: for several fixtures below the true
championship probability has a CLOSED FORM under the module's own stated
model, and the test asserts that LEVEL rather than a direction.

Three closed forms are used repeatedly:

  1. sd == 0 everywhere. `rng.normal(loc, scale=0.0)` returns `loc` on
     every draw (verified in test_playoff.py's own docstring and re-relied
     on here), so a deterministic fixture's champion is certain and
     `championship_prob` is EXACTLY 1.0 / 0.0 -- no tolerance, no flake.
  2. Exchangeability. Four IDENTICAL teams must each win the title
     exactly 1/4 of the time: every team is equally likely to occupy every
     seed and every bracket position, so no other answer is consistent
     with the symmetry of the fixture.
  3. The p-squared identity. With 4 of 4 teams in the playoffs and
     RESEEDING, a lone strong team plays two rounds against opponents drawn
     from an identical pool NO MATTER WHAT SEED IT EARNS -- so its title
     probability is exactly `p**2`, where
     `p = Phi((mu_A - mu_B) / sqrt(var_A + var_B))` is the single-game
     probability from the module's independent-Normal model. That single
     assertion simultaneously pins the bracket length, the independence of
     the two playoff weeks (reusing one week's draw for both rounds would
     give `p`, not `p**2`), and the Normal-sum moment arithmetic.

Monte Carlo tolerances are quoted in standard errors, not guessed: at
n = 40000 a probability near 0.5 has SE ~ 0.0025, so a 0.01 tolerance is
4 SE and cannot flake at any realistic rate.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from tt.league import load_config_from_dict
from tt.playoff import win_probability
from tt.season import (
    BYE,
    round_robin_schedule,
    run_bracket,
    seed_order,
    simulate_season,
    team_week_moments,
)

# A one-slot league: a team IS a single player, so every lineup decision
# collapses and the arithmetic below is hand-checkable. Same approach as
# test_playoff.py's `_ONE_WR_CONFIG`.
_ONE_WR = load_config_from_dict({
    "leagueKey": "x", "name": "x", "numTeams": 4, "maxTeams": 10,
    "draftStatus": "predraft", "rosterSlots": {"WR": 1}, "scoring": [],
})

_WR_AND_TE = load_config_from_dict({
    "leagueKey": "x", "name": "x", "numTeams": 2, "maxTeams": 10,
    "draftStatus": "predraft", "rosterSlots": {"WR": 1, "TE": 1}, "scoring": [],
})

_REAL_SHAPE = load_config_from_dict({
    "leagueKey": "x", "name": "x", "numTeams": 4, "maxTeams": 10,
    "draftStatus": "predraft",
    "rosterSlots": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1},
    "scoring": [],
})


def _wr(team: str, points: float, sd: float = 0.0, index: int = 0) -> pd.DataFrame:
    """A one-player WR roster."""
    return pd.DataFrame([{
        "player_id": f"{team}-{index}", "name": f"{team} WR{index}",
        "position": "WR", "proj_points": points, "sd": sd,
    }])


def _matchup(week: int, home: str, away: str) -> dict:
    return {"week": week, "home_team": home, "away_team": away}


def _analytic_single_game(mu_a, sd_a, mu_b, sd_b) -> float:
    return float(norm.cdf((mu_a - mu_b) / math.sqrt(sd_a**2 + sd_b**2)))


# ----------------------------------------------------------------------
# round_robin_schedule
# ----------------------------------------------------------------------

def test_round_robin_schedule_plays_every_team_exactly_once_per_week():
    schedule = round_robin_schedule(["A", "B", "C", "D"], weeks=3)
    assert list(schedule.columns) == ["week", "home_team", "away_team"]
    for week, games in schedule.groupby("week"):
        appearances = sorted(list(games["home_team"]) + list(games["away_team"]))
        assert appearances == ["A", "B", "C", "D"]


def test_round_robin_schedule_uses_every_pairing_before_repeating_any():
    schedule = round_robin_schedule(["A", "B", "C", "D"], weeks=3)
    pairs = sorted(
        tuple(sorted((row.home_team, row.away_team)))
        for row in schedule.itertuples()
    )
    assert pairs == [("A", "B"), ("A", "C"), ("A", "D"), ("B", "C"), ("B", "D"), ("C", "D")]


def test_round_robin_schedule_uses_every_pairing_once_over_six_teams_too():
    # Four teams is too small to distinguish a real circle-method rotation
    # from pairing adjacent slots -- both happen to enumerate all six pairs.
    # Six teams is not: adjacent pairing repeats (E, F) in week three.
    schedule = round_robin_schedule(list("ABCDEF"), weeks=5)
    pairs = [tuple(sorted((row.home_team, row.away_team))) for row in schedule.itertuples()]
    assert len(pairs) == 15
    assert len(set(pairs)) == 15


def test_round_robin_schedule_repeats_the_cycle_when_asked_for_more_weeks():
    # Six weeks over four teams is the three-week cycle run twice, so every
    # pairing appears exactly twice -- not some pairing three times and
    # another never.
    schedule = round_robin_schedule(["A", "B", "C", "D"], weeks=6)
    counts = pd.Series([
        tuple(sorted((row.home_team, row.away_team))) for row in schedule.itertuples()
    ]).value_counts()
    assert sorted(counts.unique()) == [2]
    assert len(counts) == 6


def test_round_robin_schedule_gives_an_odd_team_count_one_bye_per_week():
    schedule = round_robin_schedule(["A", "B", "C", "D", "E"], weeks=5)
    for week, games in schedule.groupby("week"):
        assert len(games) == 2  # five teams, two games, one idle
        appearances = list(games["home_team"]) + list(games["away_team"])
        assert len(set(appearances)) == 4


def test_round_robin_schedule_starts_at_the_week_it_is_given():
    schedule = round_robin_schedule(["A", "B"], weeks=2, start_week=5)
    assert sorted(schedule["week"].unique()) == [5, 6]


# ----------------------------------------------------------------------
# seed_order
# ----------------------------------------------------------------------

def test_seeding_ranks_by_wins_first():
    wins = np.array([[1.0, 3.0, 2.0]])
    points_for = np.array([[999.0, 1.0, 500.0]])
    assert seed_order(wins, points_for).tolist() == [[1, 2, 0]]


def test_seeding_breaks_a_win_tie_on_points_for():
    # Teams 0 and 1 tie at two wins; team 1 scored more, so it seeds ahead
    # of team 0 even though team 0 comes first in team order.
    wins = np.array([[2.0, 2.0, 1.0]])
    points_for = np.array([[100.0, 150.0, 900.0]])
    assert seed_order(wins, points_for).tolist() == [[1, 0, 2]]


def test_seeding_breaks_a_total_tie_on_team_order():
    wins = np.array([[2.0, 2.0, 2.0]])
    points_for = np.array([[100.0, 100.0, 100.0]])
    assert seed_order(wins, points_for).tolist() == [[0, 1, 2]]


def test_seeding_is_computed_independently_for_each_simulation():
    # Three teams, not two: with two the answer is symmetric enough that
    # ranking down the wrong axis produces the same array by coincidence.
    wins = np.array([[3.0, 1.0, 2.0], [1.0, 2.0, 3.0]])
    points_for = np.array([[10.0, 20.0, 30.0], [10.0, 20.0, 30.0]])
    assert seed_order(wins, points_for).tolist() == [[0, 2, 1], [2, 1, 0]]


# ----------------------------------------------------------------------
# run_bracket
# ----------------------------------------------------------------------

# An 8-team bracket in which seed 8 (team index 7) upsets seed 1 (team
# index 0) and seed 5 (index 4) upsets seed 4 (index 3), while seeds 2 and 3
# win. Survivors are therefore seeds 2, 3, 5 and 8 -- a set a fixed tree and
# a reseeded bracket pair DIFFERENTLY, which is the whole point.
_EIGHT_SEEDS = np.array([[0, 1, 2, 3, 4, 5, 6, 7]])
_UPSET_ROUND_ONE = [10.0, 50.0, 50.0, 10.0, 50.0, 10.0, 10.0, 20.0]


def _eight_team_scores() -> np.ndarray:
    scores = np.zeros((1, 3, 8))
    scores[0, 0, :] = _UPSET_ROUND_ONE
    # Later rounds: descending by team index, so nothing about rounds 2-3
    # depends on the round-1 values.
    scores[0, 1, :] = [80.0, 70.0, 60.0, 50.0, 40.0, 30.0, 20.0, 10.0]
    scores[0, 2, :] = [80.0, 70.0, 60.0, 50.0, 40.0, 30.0, 20.0, 10.0]
    return scores


def test_the_first_round_pairs_the_standard_seeded_bracket():
    result = run_bracket(_EIGHT_SEEDS, _eight_team_scores(), reseed=True)
    home, away, _ = result.rounds[0]
    # 1v8, 4v5, 2v7, 3v6 -- team indices are seed - 1.
    assert home.tolist() == [[0, 3, 1, 2]]
    assert away.tolist() == [[7, 4, 6, 5]]


def test_reseeding_pairs_the_best_surviving_seed_with_the_worst():
    result = run_bracket(_EIGHT_SEEDS, _eight_team_scores(), reseed=True)
    assert result.rounds[0][2].tolist() == [[7, 4, 1, 2]]  # winners: seeds 8, 5, 2, 3
    home, away, _ = result.rounds[1]
    # Reseeded: seed 2 (idx 1) v seed 8 (idx 7), seed 3 (idx 2) v seed 5 (idx 4).
    assert home.tolist() == [[1, 2]]
    assert away.tolist() == [[7, 4]]


def test_a_fixed_bracket_follows_the_tree_instead_of_the_seeds():
    result = run_bracket(_EIGHT_SEEDS, _eight_team_scores(), reseed=False)
    assert result.rounds[0][2].tolist() == [[7, 4, 1, 2]]  # same round-1 winners
    home, away, _ = result.rounds[1]
    # Tree order: winner(1v8) meets winner(4v5); winner(2v7) meets winner(3v6).
    assert home.tolist() == [[7, 1]]
    assert away.tolist() == [[4, 2]]


def test_reseeding_and_a_fixed_bracket_crown_different_champions_here():
    reseeded = run_bracket(_EIGHT_SEEDS, _eight_team_scores(), reseed=True)
    fixed = run_bracket(_EIGHT_SEEDS, _eight_team_scores(), reseed=False)
    # Reseeded final: winner(1v7)=1, winner(2v4)=2 -> 1 beats 2.
    assert reseeded.champion.tolist() == [1]
    # Fixed final: winner(7v4)=4, winner(1v2)=1 -> 1 beats 4. Same champion
    # would make this fixture blind to the difference, so the PAIRINGS above
    # are the load-bearing assertion; here the finalists differ.
    assert fixed.rounds[2][0].tolist() == [[4]]
    assert fixed.rounds[2][1].tolist() == [[1]]


def test_a_bracket_tie_advances_the_higher_seed():
    seeds = np.array([[3, 1]])          # team 3 is the top seed, team 1 the lower
    scores = np.array([[[0.0, 88.0, 0.0, 88.0]]])
    result = run_bracket(seeds, scores, reseed=True)
    assert result.champion.tolist() == [3]


def test_a_six_team_field_gives_the_top_two_seeds_a_bye():
    seeds = np.array([[0, 1, 2, 3, 4, 5]])
    scores = np.zeros((1, 3, 6))
    scores[0, 0, :] = [0.0, 0.0, 99.0, 10.0, 99.0, 10.0]  # 3 beats 6, 5 upsets 4
    scores[0, 1, :] = [90.0, 80.0, 70.0, 60.0, 50.0, 40.0]
    scores[0, 2, :] = [90.0, 80.0, 70.0, 60.0, 50.0, 40.0]
    result = run_bracket(seeds, scores, reseed=True)
    home, away, winner = result.rounds[0]
    assert home.tolist() == [[0, 3, 1, 2]]
    assert away.tolist() == [[BYE, 4, BYE, 5]]
    assert winner.tolist() == [[0, 4, 1, 2]]  # seeds 1 and 2 advance unplayed
    # Reseeded round two: seed 1 v seed 5, seed 2 v seed 3.
    assert result.rounds[1][0].tolist() == [[0, 1]]
    assert result.rounds[1][1].tolist() == [[4, 2]]


def test_a_bracket_too_small_for_the_field_is_rejected():
    seeds = np.array([[0, 1, 2, 3]])
    scores = np.zeros((1, 1, 4))
    with pytest.raises(ValueError, match="holds at most 2 teams"):
        run_bracket(seeds, scores, reseed=True)


# ----------------------------------------------------------------------
# team_week_moments
# ----------------------------------------------------------------------

def test_a_teams_week_is_the_optimal_lineups_mean_and_the_root_sum_of_squares_of_its_sds():
    roster = pd.DataFrame([
        {"player_id": "a", "name": "A", "position": "WR", "proj_points": 12.0, "sd": 3.0},
        {"player_id": "b", "name": "B", "position": "WR", "proj_points": 20.0, "sd": 4.0},
        {"player_id": "c", "name": "C", "position": "TE", "proj_points": 9.0, "sd": 12.0},
    ])
    moments = team_week_moments({"T": roster}, _WR_AND_TE).set_index("team")
    # The WR slot takes the 20-point WR, not the 12-point one; the TE slot
    # takes the only TE. Variances add, standard deviations do not.
    assert moments.loc["T", "exp_points"] == pytest.approx(29.0)
    assert moments.loc["T", "sd"] == pytest.approx(math.sqrt(4.0**2 + 12.0**2))
    assert moments.loc["T", "empty_slots"] == 0


def test_an_unfillable_slot_contributes_nothing_and_is_counted():
    roster = pd.DataFrame([
        {"player_id": "a", "name": "A", "position": "WR", "proj_points": 20.0, "sd": 4.0},
    ])
    moments = team_week_moments({"T": roster}, _WR_AND_TE).set_index("team")
    assert moments.loc["T", "exp_points"] == pytest.approx(20.0)
    assert moments.loc["T", "sd"] == pytest.approx(4.0)
    assert moments.loc["T", "empty_slots"] == 1


# ----------------------------------------------------------------------
# simulate_season -- validation
# ----------------------------------------------------------------------

def _four_wr_rosters(points, sd=0.0):
    return {name: _wr(name, value, sd) for name, value in points.items()}


def test_an_end_week_before_the_playoff_start_is_rejected():
    rosters = _four_wr_rosters({"A": 100.0, "B": 90.0})
    schedule = pd.DataFrame([_matchup(1, "A", "B")])
    with pytest.raises(ValueError, match="end_week"):
        simulate_season(rosters, schedule, _ONE_WR,
                        playoff_start_week=16, end_week=15, n=10, seed=1)


def test_a_regular_season_week_inside_the_playoffs_is_rejected():
    rosters = _four_wr_rosters({"A": 100.0, "B": 90.0})
    schedule = pd.DataFrame([_matchup(16, "A", "B")])
    with pytest.raises(ValueError, match="playoff_start_week"):
        simulate_season(rosters, schedule, _ONE_WR,
                        playoff_start_week=16, end_week=17, n=10, seed=1)


def test_an_unknown_team_in_the_schedule_is_rejected():
    rosters = _four_wr_rosters({"A": 100.0, "B": 90.0})
    schedule = pd.DataFrame([_matchup(1, "A", "Z")])
    # The message must be the HELPFUL one, not the bare KeyError a raw dict
    # lookup would raise several steps later.
    with pytest.raises(KeyError, match="no roster"):
        simulate_season(rosters, schedule, _ONE_WR,
                        playoff_start_week=2, end_week=2, n=10, seed=1)


def test_a_team_playing_twice_in_one_week_is_rejected():
    rosters = _four_wr_rosters({"A": 100.0, "B": 90.0, "C": 80.0})
    schedule = pd.DataFrame([_matchup(1, "A", "B"), _matchup(1, "A", "C")])
    with pytest.raises(ValueError, match="twice"):
        simulate_season(rosters, schedule, _ONE_WR,
                        playoff_start_week=2, end_week=3, n=10, seed=1)


def test_a_playoff_field_bigger_than_the_league_is_rejected():
    rosters = _four_wr_rosters({"A": 100.0, "B": 90.0})
    schedule = pd.DataFrame([_matchup(1, "A", "B")])
    with pytest.raises(ValueError, match="between 1 and the 2 teams"):
        simulate_season(rosters, schedule, _ONE_WR, playoff_teams=4,
                        playoff_start_week=2, end_week=2, n=10, seed=1)


def test_a_playoff_field_that_cannot_be_reduced_to_a_champion_is_rejected():
    # Four teams and only one playoff week: a legal field size, but not one a
    # single elimination round can settle. Weeks 16-17 give two rounds and
    # therefore room for four; a league that fills to 10 would need three.
    rosters = _four_wr_rosters({"A": 100.0, "B": 90.0, "C": 80.0, "D": 70.0})
    schedule = round_robin_schedule(["A", "B", "C", "D"], weeks=1)
    with pytest.raises(ValueError, match="cannot be reduced to a champion"):
        simulate_season(rosters, schedule, _ONE_WR,
                        playoff_start_week=2, end_week=2, n=10, seed=1)


# ----------------------------------------------------------------------
# simulate_season -- exact, deterministic cases
# ----------------------------------------------------------------------

def test_two_deterministic_teams_produce_a_certain_champion():
    rosters = _four_wr_rosters({"A": 100.0, "B": 90.0}, sd=0.0)
    schedule = pd.DataFrame([_matchup(1, "A", "B")])
    result = simulate_season(rosters, schedule, _ONE_WR,
                             playoff_start_week=2, end_week=2,
                             n=500, seed=7).set_index("team")
    assert result.loc["A", "championship_prob"] == 1.0
    assert result.loc["B", "championship_prob"] == 0.0
    assert result.loc["A", "expected_wins"] == 1.0
    assert result.loc["B", "expected_wins"] == 0.0


def test_expected_wins_and_points_for_are_exact_for_deterministic_rosters():
    rosters = _four_wr_rosters({"A": 100.0, "B": 90.0, "C": 80.0, "D": 70.0}, sd=0.0)
    schedule = round_robin_schedule(["A", "B", "C", "D"], weeks=3)
    result = simulate_season(rosters, schedule, _ONE_WR,
                             playoff_start_week=4, end_week=5,
                             n=200, seed=3).set_index("team")
    assert result["expected_wins"].to_dict() == {"A": 3.0, "B": 2.0, "C": 1.0, "D": 0.0}
    assert result["mean_points_for"].to_dict() == {"A": 300.0, "B": 270.0, "C": 240.0, "D": 210.0}
    assert result["mean_seed"].to_dict() == {"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0}
    assert result["p_seed_1"].to_dict() == {"A": 1.0, "B": 0.0, "C": 0.0, "D": 0.0}
    # 4 of 4 make the playoffs: the objective everywhere is the title, and
    # qualification is a constant carrying no information.
    assert set(result["p_playoffs"]) == {1.0}


def test_the_seeding_tiebreak_is_points_for_not_the_order_teams_were_listed():
    # A and B both go 2-0; C and D both go 0-2. Within each pair the
    # tiebreak must be points-for. The rosters are deliberately listed
    # B, A, D, C so that a points-for tiebreak and a list-order tiebreak
    # disagree on every seed.
    rosters = {
        "B": _wr("B", 99.0), "A": _wr("A", 100.0),
        "D": _wr("D", 1.0), "C": _wr("C", 98.0),
    }
    schedule = pd.DataFrame([
        _matchup(1, "A", "D"), _matchup(1, "B", "C"),
        _matchup(2, "A", "D"), _matchup(2, "B", "C"),
    ])
    result = simulate_season(rosters, schedule, _ONE_WR,
                             playoff_start_week=3, end_week=4,
                             n=200, seed=5).set_index("team")
    assert result["mean_seed"].to_dict() == {"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0}


def test_an_unfillable_slot_costs_a_team_the_title():
    # Two teams whose WRs are identical; one also has a TE. With sd = 0 the
    # empty TE slot is the entire margin, and it contributes exactly zero
    # rather than being silently skipped.
    rosters = {
        "Full": pd.DataFrame([
            {"player_id": "f1", "name": "F1", "position": "WR", "proj_points": 20.0, "sd": 0.0},
            {"player_id": "f2", "name": "F2", "position": "TE", "proj_points": 1.0, "sd": 0.0},
        ]),
        "Thin": pd.DataFrame([
            {"player_id": "t1", "name": "T1", "position": "WR", "proj_points": 20.0, "sd": 0.0},
        ]),
    }
    schedule = pd.DataFrame([_matchup(1, "Full", "Thin")])
    result = simulate_season(rosters, schedule, _WR_AND_TE,
                             playoff_start_week=2, end_week=2,
                             n=200, seed=11).set_index("team")
    assert result.loc["Full", "championship_prob"] == 1.0
    assert result.loc["Thin", "championship_prob"] == 0.0
    assert result.loc["Thin", "empty_slots"] == 1
    assert result.loc["Thin", "mean_points_for"] == 20.0


# ----------------------------------------------------------------------
# simulate_season -- closed-form probabilistic cases
# ----------------------------------------------------------------------

def test_four_identical_teams_each_win_exactly_a_quarter_of_the_titles():
    rosters = _four_wr_rosters({"A": 120.0, "B": 120.0, "C": 120.0, "D": 120.0}, sd=30.0)
    schedule = round_robin_schedule(["A", "B", "C", "D"], weeks=6)
    result = simulate_season(rosters, schedule, _ONE_WR,
                             playoff_start_week=7, end_week=8,
                             n=40000, seed=17).set_index("team")
    # Exchangeable teams, so 0.25 each is the only answer consistent with
    # the fixture's symmetry. SE at n=40000 is 0.0022; 0.01 is ~4.6 SE.
    for team in ("A", "B", "C", "D"):
        assert result.loc[team, "championship_prob"] == pytest.approx(0.25, abs=0.01)
        assert result.loc[team, "p_seed_1"] == pytest.approx(0.25, abs=0.01)
    assert result["championship_prob"].sum() == pytest.approx(1.0)


def test_a_dominant_team_wins_the_square_of_its_single_game_probability():
    # 4 of 4 make the playoffs and the bracket reseeds, so team A plays two
    # rounds against members of an identical pool whatever seed it earns.
    # Its title probability is therefore exactly p**2 -- high, and provably
    # nowhere near certainty.
    rosters = _four_wr_rosters({"A": 150.0, "B": 120.0, "C": 120.0, "D": 120.0}, sd=30.0)
    schedule = round_robin_schedule(["A", "B", "C", "D"], weeks=6)
    result = simulate_season(rosters, schedule, _ONE_WR,
                             playoff_start_week=7, end_week=8,
                             n=40000, seed=23).set_index("team")

    p = _analytic_single_game(150.0, 30.0, 120.0, 30.0)
    assert p == pytest.approx(0.760250, abs=1e-5)
    assert result.loc["A", "championship_prob"] == pytest.approx(p * p, abs=0.01)
    assert result.loc["A", "championship_prob"] < 0.65  # dominant, not certain
    for team in ("B", "C", "D"):
        assert result.loc[team, "championship_prob"] == pytest.approx((1 - p * p) / 3, abs=0.01)


def test_the_two_playoff_weeks_are_independent_draws_not_one_reused_week():
    # Same fixture as above. If both playoff rounds reused a single week's
    # draw, the round-one winner would win round two by construction and A's
    # title probability would be p, not p**2. This asserts the gap directly.
    rosters = _four_wr_rosters({"A": 150.0, "B": 120.0, "C": 120.0, "D": 120.0}, sd=30.0)
    schedule = round_robin_schedule(["A", "B", "C", "D"], weeks=6)
    result = simulate_season(rosters, schedule, _ONE_WR,
                             playoff_start_week=7, end_week=8,
                             n=40000, seed=23).set_index("team")
    p = _analytic_single_game(150.0, 30.0, 120.0, 30.0)
    assert result.loc["A", "championship_prob"] < p - 0.1


def test_a_single_round_title_probability_matches_playoff_win_probability():
    # The bracket's head-to-head must be the SAME model `tt.playoff` already
    # documents: independent Normal per starter, summed, strict >. With one
    # playoff round and two teams, championship_prob IS win_probability.
    rosters = _four_wr_rosters({"A": 100.0, "B": 95.0})
    rosters["A"] = _wr("A", 100.0, 20.0)
    rosters["B"] = _wr("B", 95.0, 15.0)
    schedule = pd.DataFrame([_matchup(1, "A", "B")])
    result = simulate_season(rosters, schedule, _ONE_WR,
                             playoff_start_week=2, end_week=2,
                             n=40000, seed=29).set_index("team")

    analytic = _analytic_single_game(100.0, 20.0, 95.0, 15.0)
    assert analytic == pytest.approx(0.579260, abs=1e-5)
    assert result.loc["A", "championship_prob"] == pytest.approx(analytic, abs=0.01)
    # And against the existing module, run on the very same lineups.
    from_playoff = win_probability(rosters["A"], rosters["B"], n=40000, seed=29)
    assert result.loc["A", "championship_prob"] == pytest.approx(from_playoff, abs=0.015)


def test_every_team_has_a_real_title_chance_when_everyone_makes_the_playoffs():
    # Four teams spanning a huge talent gap. num_playoff_teams == num_teams,
    # so even the worst roster in the league is two single-week coin-flips
    # from a title and its probability must be strictly positive.
    rosters = _four_wr_rosters({"A": 160.0, "B": 130.0, "C": 110.0, "D": 80.0}, sd=25.0)
    schedule = round_robin_schedule(["A", "B", "C", "D"], weeks=6)
    result = simulate_season(rosters, schedule, _ONE_WR,
                             playoff_start_week=7, end_week=8,
                             n=40000, seed=31).set_index("team")
    assert (result["championship_prob"] > 0.0).all()
    assert result["championship_prob"].sum() == pytest.approx(1.0)
    # D is a 160-vs-80 underdog needing two single-week upsets: tiny, and
    # firmly non-zero. SE at n=40000 for p ~ 0.0014 is 0.00019, so 0.0008 is
    # about 4 SE.
    assert result.loc["D", "championship_prob"] == pytest.approx(0.001425, abs=0.0008)
    assert result.loc["A", "championship_prob"] == pytest.approx(0.81675, abs=0.01)
    # Making the playoffs is guaranteed and therefore says nothing.
    assert set(result["p_playoffs"]) == {1.0}


def test_reaching_the_final_is_more_likely_than_winning_it():
    rosters = _four_wr_rosters({"A": 160.0, "B": 130.0, "C": 110.0, "D": 80.0}, sd=25.0)
    schedule = round_robin_schedule(["A", "B", "C", "D"], weeks=6)
    result = simulate_season(rosters, schedule, _ONE_WR,
                             playoff_start_week=7, end_week=8,
                             n=20000, seed=37).set_index("team")
    assert (result["p_final"] > result["championship_prob"]).all()
    # Exactly two teams reach a two-team final in every simulation.
    assert result["p_final"].sum() == pytest.approx(2.0)


# ----------------------------------------------------------------------
# simulate_season -- the "one world per week" property
# ----------------------------------------------------------------------

def test_a_teams_week_score_does_not_depend_on_who_it_played():
    # Both sides of a matchup read the SAME draw of that week's world, so a
    # team's own points-for is a property of (simulation, week, team) alone.
    # Re-pairing the identical weeks must leave every team's points-for
    # bit-for-bit unchanged -- which is false if each matchup draws its two
    # sides from its own place in the random stream.
    rosters = _four_wr_rosters({"A": 130.0, "B": 120.0, "C": 110.0, "D": 100.0}, sd=25.0)
    one = pd.DataFrame([
        _matchup(1, "A", "B"), _matchup(1, "C", "D"),
        _matchup(2, "A", "C"), _matchup(2, "B", "D"),
    ])
    two = pd.DataFrame([
        _matchup(1, "A", "D"), _matchup(1, "B", "C"),
        _matchup(2, "A", "B"), _matchup(2, "C", "D"),
    ])
    kwargs = dict(playoff_start_week=3, end_week=4, n=3000, seed=41)
    first = simulate_season(rosters, one, _ONE_WR, **kwargs).set_index("team")
    second = simulate_season(rosters, two, _ONE_WR, **kwargs).set_index("team")
    for team in ("A", "B", "C", "D"):
        assert first.loc[team, "mean_points_for"] == second.loc[team, "mean_points_for"]


# ----------------------------------------------------------------------
# determinism
# ----------------------------------------------------------------------

def test_the_same_seed_gives_the_same_answer():
    rosters = _four_wr_rosters({"A": 130.0, "B": 120.0, "C": 110.0, "D": 100.0}, sd=25.0)
    schedule = round_robin_schedule(["A", "B", "C", "D"], weeks=3)
    kwargs = dict(playoff_start_week=4, end_week=5, n=2000, seed=53)
    first = simulate_season(rosters, schedule, _ONE_WR, **kwargs)
    second = simulate_season(rosters, schedule, _ONE_WR, **kwargs)
    pd.testing.assert_frame_equal(first, second)


def test_a_different_seed_gives_a_different_answer():
    rosters = _four_wr_rosters({"A": 130.0, "B": 120.0, "C": 110.0, "D": 100.0}, sd=25.0)
    schedule = round_robin_schedule(["A", "B", "C", "D"], weeks=3)
    kwargs = dict(playoff_start_week=4, end_week=5, n=2000)
    first = simulate_season(rosters, schedule, _ONE_WR, seed=53, **kwargs)
    second = simulate_season(rosters, schedule, _ONE_WR, seed=54, **kwargs)
    assert not first["championship_prob"].equals(second["championship_prob"])


def test_the_result_carries_the_run_parameters():
    rosters = _four_wr_rosters({"A": 130.0, "B": 120.0})
    schedule = pd.DataFrame([_matchup(1, "A", "B")])
    result = simulate_season(rosters, schedule, _ONE_WR,
                             playoff_start_week=2, end_week=2, n=250, seed=59)
    assert result.attrs["n"] == 250
    assert result.attrs["seed"] == 59
    assert result.attrs["playoff_rounds"] == 1
    assert result.attrs["regular_season_weeks"] == 1
    assert result.attrs["playoff_teams"] == 2
    assert result.attrs["reseed"] is True
    # 1 / (2 * sqrt(n)) is the worst-case standard error of any probability
    # in the table -- reported so a caller can tell signal from noise.
    assert result.attrs["monte_carlo_se"] == pytest.approx(0.5 / math.sqrt(250))


# ----------------------------------------------------------------------
# the weekly-projection join: conditional vs marginal
# ----------------------------------------------------------------------

def _weekly_frame() -> pd.DataFrame:
    """`weekly.project_week`-shaped output for two coin-flip players."""
    return pd.DataFrame([
        {"player_id": "a", "name": "A", "position": "WR",
         "exp_points": 20.0, "sd": 0.0, "p_active": 0.5,
         "exp_points_marginal": 10.0, "sd_marginal": 0.0},
        {"player_id": "b", "name": "B", "position": "WR",
         "exp_points": 18.0, "sd": 0.0, "p_active": 0.5,
         "exp_points_marginal": 9.0, "sd_marginal": 0.0},
    ])


def test_the_marginal_view_prices_in_the_weeks_a_player_does_not_play():
    rosters = {
        "A": pd.DataFrame([{"player_id": "a"}]),
        "B": pd.DataFrame([{"player_id": "b"}]),
    }
    schedule = pd.DataFrame([_matchup(1, "A", "B"), _matchup(2, "A", "B")])
    kwargs = dict(playoff_start_week=3, end_week=3, n=100, seed=61)
    marginal = simulate_season(rosters, schedule, _ONE_WR, weekly=_weekly_frame(),
                               marginal=True, **kwargs).set_index("team")
    conditional = simulate_season(rosters, schedule, _ONE_WR, weekly=_weekly_frame(),
                                  marginal=False, **kwargs).set_index("team")
    assert marginal.loc["A", "mean_points_for"] == pytest.approx(20.0)       # 10 x 2 weeks
    assert conditional.loc["A", "mean_points_for"] == pytest.approx(40.0)    # 20 x 2 weeks
    assert marginal.loc["B", "mean_points_for"] == pytest.approx(18.0)
    assert conditional.loc["B", "mean_points_for"] == pytest.approx(36.0)


def test_a_roster_player_with_no_weekly_projection_cannot_start():
    # K and DEF get no projection from this offense-only pipeline, so their
    # slots are named empty rather than filled with a guess. The unprojected
    # player here is deliberately given an ELIGIBLE position and a slot going
    # begging, so the only thing keeping it out of the lineup is its missing
    # projection -- if the join quietly filled that with 0.0, it would start.
    two_wr = load_config_from_dict({
        "leagueKey": "x", "name": "x", "numTeams": 2, "maxTeams": 10,
        "draftStatus": "predraft", "rosterSlots": {"WR": 2}, "scoring": [],
    })
    rosters = {
        "A": pd.DataFrame([
            {"player_id": "a", "position": "WR"},
            {"player_id": "unprojected", "position": "WR"},
        ]),
        "B": pd.DataFrame([{"player_id": "b", "position": "WR"}]),
    }
    schedule = pd.DataFrame([_matchup(1, "A", "B")])
    result = simulate_season(rosters, schedule, two_wr, weekly=_weekly_frame(),
                             marginal=False, playoff_start_week=2, end_week=2,
                             n=100, seed=67).set_index("team")
    assert result.loc["A", "mean_points_for"] == pytest.approx(20.0)
    assert result.loc["A", "empty_slots"] == 1


def test_a_tied_week_is_half_a_win_to_each_side_and_a_tied_final_goes_to_the_seed():
    # Two identical deterministic teams draw every week, so each banks half a
    # win per week. The title game is a draw too, and the higher seed takes
    # it -- with the standings identical, that seed is settled by the order
    # the teams were listed, which is the documented final backstop.
    rosters = _four_wr_rosters({"A": 100.0, "B": 100.0}, sd=0.0)
    schedule = pd.DataFrame([_matchup(1, "A", "B"), _matchup(2, "A", "B")])
    result = simulate_season(rosters, schedule, _ONE_WR,
                             playoff_start_week=3, end_week=3,
                             n=200, seed=73).set_index("team")
    assert result["expected_wins"].to_dict() == {"A": 1.0, "B": 1.0}
    assert result.loc["A", "mean_seed"] == 1.0
    assert result.loc["B", "mean_seed"] == 2.0
    assert result.loc["A", "championship_prob"] == 1.0
    assert result.loc["B", "championship_prob"] == 0.0


# ----------------------------------------------------------------------
# a full-shape league, to prove the module is not tied to a one-slot toy
# ----------------------------------------------------------------------

def _full_roster(team: str, level: float, rng: np.random.Generator) -> pd.DataFrame:
    rows = []
    for position, count in (("QB", 2), ("RB", 4), ("WR", 5), ("TE", 2)):
        for i in range(count):
            mean = level * (1.0 - 0.12 * i)
            rows.append({
                "player_id": f"{team}-{position}{i}", "name": f"{team} {position}{i}",
                "position": position, "proj_points": mean, "sd": mean * 0.6,
            })
    return pd.DataFrame(rows)


def test_the_real_league_shape_runs_and_the_probabilities_sum_to_one():
    rng = np.random.default_rng(0)
    levels = {"A": 16.0, "B": 15.0, "C": 14.0, "D": 13.0}
    rosters = {team: _full_roster(team, level, rng) for team, level in levels.items()}
    schedule = round_robin_schedule(list(rosters), weeks=15)
    result = simulate_season(rosters, schedule, _REAL_SHAPE,
                             playoff_start_week=16, end_week=17,
                             n=5000, seed=71).set_index("team")
    assert result["championship_prob"].sum() == pytest.approx(1.0)
    assert (result["championship_prob"] > 0.05).all()
    assert result.loc["A", "championship_prob"] > result.loc["D", "championship_prob"]
    # K and DEF are unprojectable here, so two slots per team are empty --
    # visible in the output, not silently absorbed.
    assert set(result["empty_slots"]) == {2}
