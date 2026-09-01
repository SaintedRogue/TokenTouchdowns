"""Trade valuation (`tt.trade`).

WHAT A TRADE IS WORTH, AND WHY THESE TESTS ASSERT LEVELS. A trade is worth
the change it makes to CHAMPIONSHIP PROBABILITY, so every number below is
a difference of two probabilities -- the quantity most vulnerable to being
read out of Monte Carlo noise. The tests are therefore built so the true
answer is KNOWN, not merely plausible:

  1. THE TWO-TEAM CLOSED FORM. With two teams and a single playoff round,
     `championship_prob` is exactly `Phi((mu_A - mu_B) / sqrt(var_A +
     var_B))` -- there is no bracket to shuffle and no seed to earn. Both
     sides' deltas are then analytic, and they must sum to zero exactly
     because one of the two teams always wins.
  2. THE DETERMINISTIC FLIP. With `sd = 0` everywhere the champion is
     certain, so a trade that reverses which team is stronger moves the
     title probability by EXACTLY 1.0 with a paired standard error of
     EXACTLY 0.0 -- and that zero is what separates paired (common random
     number) arithmetic from an independent-samples standard error, which
     would be nonzero for the same numbers.
  3. THE ALGEBRAIC STANDARD ERROR. `worlds_gained` and `worlds_lost` are
     reported, and the paired standard error is a closed-form function of
     just those two counts and `n`. The test recomputes it from the
     reported counts and demands equality to floating-point precision, so
     no other standard-error formula can survive.

COMMON RANDOM NUMBERS ARE THE POINT. Two tests demand EXACTLY 0.0 (an
empty trade, and a player swapped for an identical twin). Under
independent draws before and after, both would be small-but-nonzero at
every `n`; only paired draws make them exact.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from tt.league import load_config_from_dict
from tt.season import round_robin_schedule, simulate_season
from tt.trade import apply_trade, evaluate_trade, find_trades

# A one-slot league: a team IS a single player, so every lineup decision
# collapses and the arithmetic is hand-checkable. Same device as
# test_season.py's `_ONE_WR` and test_playoff.py's `_ONE_WR_CONFIG`.
_ONE_WR = load_config_from_dict({
    "leagueKey": "x", "name": "x", "numTeams": 4, "maxTeams": 10,
    "draftStatus": "predraft", "rosterSlots": {"WR": 1}, "scoring": [],
})

# One QB and two RBs: the smallest league in which hoarding a position is a
# real, measurable mistake -- a second QB can never enter the lineup.
_QB_RB = load_config_from_dict({
    "leagueKey": "x", "name": "x", "numTeams": 4, "maxTeams": 10,
    "draftStatus": "predraft", "rosterSlots": {"QB": 1, "RB": 2}, "scoring": [],
})


def _roster(*players: tuple[str, str, float, float]) -> pd.DataFrame:
    """`(player_id, position, proj_points, sd)` tuples -> a roster frame."""
    return pd.DataFrame(
        [{"player_id": pid, "name": pid.upper(), "position": position,
          "proj_points": points, "sd": sd} for pid, position, points, sd in players]
    )


def _two_team_league() -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Two teams, one WR slot each, a three-week regular season and a
    ONE-round playoff in week 4. Championship probability has a closed form
    here (see the module docstring)."""
    rosters = {
        "A": _roster(("a1", "WR", 90.0, 20.0), ("a2", "WR", 60.0, 20.0)),
        "B": _roster(("b1", "WR", 100.0, 20.0), ("b2", "WR", 88.0, 20.0)),
    }
    return rosters, round_robin_schedule(["A", "B"], weeks=3)


def _single_game(mu_a: float, sd_a: float, mu_b: float, sd_b: float) -> float:
    return float(norm.cdf((mu_a - mu_b) / math.sqrt(sd_a ** 2 + sd_b ** 2)))


def _row(result: pd.DataFrame, team: str) -> pd.Series:
    (row,) = [r for _, r in result.iterrows() if r["team"] == team]
    return row


# ----------------------------------------------------------------------
# apply_trade -- the swap itself
# ----------------------------------------------------------------------

def test_apply_trade_moves_exactly_the_named_players_both_ways():
    rosters, _ = _two_team_league()
    after = apply_trade(rosters, "A", "B", ["a1"], ["b1"])
    assert sorted(after["A"]["player_id"]) == ["a2", "b1"]
    assert sorted(after["B"]["player_id"]) == ["a1", "b2"]


def test_apply_trade_leaves_the_original_rosters_untouched():
    rosters, _ = _two_team_league()
    apply_trade(rosters, "A", "B", ["a1"], ["b1"])
    assert sorted(rosters["A"]["player_id"]) == ["a1", "a2"]
    assert sorted(rosters["B"]["player_id"]) == ["b1", "b2"]


def test_apply_trade_keeps_every_other_team_and_the_team_order():
    rosters = {
        "A": _roster(("a1", "WR", 90.0, 20.0)),
        "C": _roster(("c1", "WR", 70.0, 20.0)),
        "B": _roster(("b1", "WR", 100.0, 20.0)),
    }
    after = apply_trade(rosters, "A", "B", ["a1"], ["b1"])
    # Order matters: `simulate_season` indexes teams by `list(rosters)`, and
    # common random numbers are only common if that index is stable.
    assert list(after) == ["A", "C", "B"]
    assert after["C"] is rosters["C"]


def test_apply_trade_rejects_a_player_the_giving_team_does_not_hold():
    rosters, _ = _two_team_league()
    with pytest.raises(KeyError, match="b1"):
        apply_trade(rosters, "A", "B", ["b1"], ["b2"])


def test_apply_trade_rejects_a_player_the_receiving_team_does_not_hold():
    rosters, _ = _two_team_league()
    with pytest.raises(KeyError, match="a2"):
        apply_trade(rosters, "A", "B", ["a1"], ["a2"])


def test_apply_trade_rejects_trading_with_yourself():
    rosters, _ = _two_team_league()
    with pytest.raises(ValueError, match="itself"):
        apply_trade(rosters, "A", "A", ["a1"], ["a2"])


def test_apply_trade_rejects_an_unknown_team():
    rosters, _ = _two_team_league()
    # Matching on the id alone would pass against a DELETED check -- an
    # unvalidated `rosters["Z"]` raises KeyError("Z") by itself. The message
    # is what proves the check ran.
    with pytest.raises(KeyError, match="no roster for team"):
        apply_trade(rosters, "A", "Z", ["a1"], [])


def test_apply_trade_rejects_the_same_player_named_twice():
    rosters, _ = _two_team_league()
    with pytest.raises(ValueError, match="twice"):
        apply_trade(rosters, "A", "B", ["a1", "a1"], ["b1"])


# ----------------------------------------------------------------------
# evaluate_trade -- the closed forms
# ----------------------------------------------------------------------

def _evaluate(rosters, schedule, give, get, **kwargs) -> pd.DataFrame:
    defaults = dict(
        my_team="A", their_team="B", i_give=give, i_get=get,
        playoff_start_week=4, end_week=4, n=40_000, seed=515,
    )
    defaults.update(kwargs)
    return evaluate_trade(rosters, schedule, _ONE_WR, **defaults)


def test_a_two_team_trade_moves_both_sides_by_the_closed_form_amount():
    # A starts a1 (90, 20), B starts b1 (100, 20); the trade swaps them, so
    # A's single-game win probability goes from Phi(-10/sqrt(800)) to
    # Phi(+10/sqrt(800)) -- and with two teams and one playoff round that
    # IS the championship probability, with no bracket in between.
    rosters, schedule = _two_team_league()
    before = _single_game(90.0, 20.0, 100.0, 20.0)
    after = _single_game(100.0, 20.0, 90.0, 20.0)

    result = _evaluate(rosters, schedule, ["a1"], ["b1"])
    mine, theirs = _row(result, "A"), _row(result, "B")

    # 0.01 is 4 standard errors at n = 40000 for a probability near 0.5.
    assert mine["championship_prob_before"] == pytest.approx(before, abs=0.01)
    assert mine["championship_prob_after"] == pytest.approx(after, abs=0.01)
    assert mine["delta"] == pytest.approx(after - before, abs=0.01)
    assert theirs["championship_prob_before"] == pytest.approx(1.0 - before, abs=0.01)
    assert theirs["delta"] == pytest.approx(before - after, abs=0.01)


def test_the_two_sides_deltas_sum_to_exactly_zero_in_a_two_team_league():
    # One of the two teams wins every simulated season, so the two title
    # probabilities sum to 1 in every world -- exactly, not approximately.
    rosters, schedule = _two_team_league()
    result = _evaluate(rosters, schedule, ["a1"], ["b1"])
    assert _row(result, "A")["delta"] + _row(result, "B")["delta"] == 0.0


def test_an_empty_trade_moves_nothing_at_all():
    # THE COMMON-RANDOM-NUMBER TEST. Under independent before/after draws
    # this would be a small nonzero number at every n; paired draws make it
    # exactly zero.
    rosters, schedule = _two_team_league()
    result = _evaluate(rosters, schedule, [], [])
    for team in ("A", "B"):
        assert _row(result, team)["delta"] == 0.0
        assert _row(result, team)["delta_se"] == 0.0
        assert _row(result, team)["worlds_gained"] == 0
        assert _row(result, team)["worlds_lost"] == 0
        assert not _row(result, team)["significant"]


def test_trading_a_player_for_his_identical_twin_is_exactly_zero():
    # b3 is a1 in every respect that the model can see. Swapping them is a
    # no-op, and with paired draws the measured no-op is EXACT.
    rosters = {
        "A": _roster(("a1", "WR", 90.0, 20.0), ("a2", "WR", 60.0, 20.0)),
        "B": _roster(("b1", "WR", 100.0, 20.0), ("b3", "WR", 90.0, 20.0)),
    }
    schedule = round_robin_schedule(["A", "B"], weeks=3)
    result = _evaluate(rosters, schedule, ["a1"], ["b3"])
    for team in ("A", "B"):
        assert _row(result, team)["delta"] == 0.0
        assert _row(result, team)["delta_se"] == 0.0
    assert _row(result, "A")["exp_points_after"] == _row(result, "A")["exp_points_before"]


def test_a_deterministic_flip_is_exactly_one_with_exactly_zero_standard_error():
    # sd = 0 everywhere, so every simulated world is identical: B wins the
    # title with certainty before the trade and A with certainty after.
    # An INDEPENDENT-samples standard error of two probabilities measured at
    # 0.0 and 1.0 would be 0 as well -- but `worlds_gained` pins the count,
    # and `test_the_standard_error_is_the_paired_one` below pins the formula.
    rosters = {
        "A": _roster(("a1", "WR", 90.0, 0.0)),
        "B": _roster(("b1", "WR", 100.0, 0.0)),
    }
    schedule = round_robin_schedule(["A", "B"], weeks=3)
    result = _evaluate(rosters, schedule, ["a1"], ["b1"], n=500)
    mine, theirs = _row(result, "A"), _row(result, "B")
    assert mine["championship_prob_before"] == 0.0
    assert mine["championship_prob_after"] == 1.0
    assert mine["delta"] == 1.0
    assert mine["delta_se"] == 0.0
    assert mine["worlds_gained"] == 500
    assert mine["worlds_lost"] == 0
    assert bool(mine["significant"]) is True
    assert theirs["delta"] == -1.0
    assert theirs["worlds_lost"] == 500


def test_the_standard_error_is_the_paired_one_computed_from_the_flip_counts():
    # The reported standard error must be the sample standard error of the
    # PAIRED difference D_i = 1{champion_after == me} - 1{champion_before ==
    # me}, which takes values in {-1, 0, +1}. Since sum(D^2) = gained + lost
    # and sum(D) = gained - lost, that standard error is a closed-form
    # function of the two reported counts -- recomputed here exactly, so no
    # other standard-error formula (an independent-samples one above all)
    # can pass.
    #
    # The fixture trades A's starter a1 (90) for B's BENCH b2 (88): a real
    # but small downgrade for A, so the champion genuinely changes in some
    # worlds and not in most -- which is the regime the paired standard
    # error exists for.
    rosters, schedule = _two_team_league()
    n = 20_000
    result = _evaluate(rosters, schedule, ["a1"], ["b2"], n=n)
    for team in ("A", "B"):
        row = _row(result, team)
        gained, lost = int(row["worlds_gained"]), int(row["worlds_lost"])
        assert gained + lost > 0, "fixture must actually flip some worlds"
        total = float(gained - lost)
        variance = ((gained + lost) - total ** 2 / n) / (n - 1)
        assert row["delta"] == pytest.approx(total / n, rel=1e-12)
        assert row["delta_se"] == pytest.approx(math.sqrt(variance / n), rel=1e-12)


def test_the_paired_standard_error_is_far_smaller_than_the_unpaired_one():
    # The whole reason for common random numbers: the before/after title
    # probabilities are each near 0.35, so an INDEPENDENT-samples standard
    # error on their difference is ~0.0048 at n = 20000 -- but they move
    # together world by world, so the paired one is several times smaller.
    rosters, schedule = _two_team_league()
    n = 20_000
    row = _row(_evaluate(rosters, schedule, ["a1"], ["b2"], n=n), "A")
    unpaired = math.sqrt(
        row["championship_prob_before"] * (1 - row["championship_prob_before"]) / n
        + row["championship_prob_after"] * (1 - row["championship_prob_after"]) / n
    )
    assert row["delta_se"] < unpaired / 3


def test_a_lineup_neutral_trade_is_reported_as_no_change_at_all():
    # a2 (60) for b2 (88) in a one-WR league: A still starts a1 (90) and B
    # still starts b1 (100), so NEITHER lineup moves. The measured delta is
    # exactly zero -- not "small" -- and must not be dressed up as signal.
    rosters, schedule = _two_team_league()
    row = _row(_evaluate(rosters, schedule, ["a2"], ["b2"], n=20_000), "A")
    assert row["exp_points_delta"] == 0.0
    assert row["delta"] == 0.0
    assert not row["significant"]
    assert row["delta_ci_low"] <= 0.0 <= row["delta_ci_high"]


def test_a_real_edge_outside_the_noise_is_reported_as_significant():
    # A trades its starter a1 (90) for b2 (88): a genuine two-point
    # downgrade. The two-team closed form puts the delta at
    # Phi(-12/sqrt(800)) - Phi(-10/sqrt(800)) = -0.0261, which at n = 20000
    # is many paired standard errors from zero -- so `significant` must be
    # True here, and the whole interval must sit below zero.
    rosters, schedule = _two_team_league()
    row = _row(_evaluate(rosters, schedule, ["a1"], ["b2"], n=20_000), "A")
    expected = _single_game(88.0, 20.0, 100.0, 20.0) - _single_game(90.0, 20.0, 100.0, 20.0)
    assert row["delta"] == pytest.approx(expected, abs=0.01)
    assert bool(row["significant"]) is True
    assert row["delta_ci_high"] < 0.0


def test_the_confidence_interval_is_the_delta_plus_or_minus_two_standard_errors():
    rosters, schedule = _two_team_league()
    row = _row(_evaluate(rosters, schedule, ["a1"], ["b1"], n=20_000), "A")
    z = 1.959963984540054
    assert row["delta_ci_low"] == pytest.approx(row["delta"] - z * row["delta_se"], rel=1e-12)
    assert row["delta_ci_high"] == pytest.approx(row["delta"] + z * row["delta_se"], rel=1e-12)


# ----------------------------------------------------------------------
# evaluate_trade -- agreement with the season simulator it is built on
# ----------------------------------------------------------------------

def _four_team_league() -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    rosters = {
        "A": _roster(("a1", "WR", 90.0, 20.0), ("a2", "WR", 60.0, 15.0)),
        "B": _roster(("b1", "WR", 100.0, 22.0), ("b2", "WR", 55.0, 12.0)),
        "C": _roster(("c1", "WR", 85.0, 18.0), ("c2", "WR", 70.0, 14.0)),
        "D": _roster(("d1", "WR", 80.0, 25.0), ("d2", "WR", 65.0, 16.0)),
    }
    return rosters, round_robin_schedule(list(rosters), weeks=9)


def test_the_before_column_reproduces_simulate_season_to_the_last_digit():
    # `evaluate_trade` re-implements the simulation loop so it can hold on
    # to the per-world champion (which `simulate_season` aggregates away).
    # This pins that the re-implementation is not a second, drifting model:
    # same seed, same n, same numbers, exactly.
    rosters, schedule = _four_team_league()
    reference = simulate_season(
        rosters, schedule, _ONE_WR, playoff_start_week=10, end_week=11,
        n=5_000, seed=99,
    ).set_index("team")
    result = evaluate_trade(
        rosters, schedule, _ONE_WR, my_team="A", their_team="B",
        i_give=["a1"], i_get=["b1"], playoff_start_week=10, end_week=11,
        n=5_000, seed=99,
    )
    for team in ("A", "B"):
        row = _row(result, team)
        assert row["championship_prob_before"] == reference.loc[team, "championship_prob"]
        assert row["expected_wins_before"] == reference.loc[team, "expected_wins"]
        assert row["p_seed_1_before"] == reference.loc[team, "p_seed_1"]
        assert row["exp_points_before"] == reference.loc[team, "exp_points_per_week"]
        assert row["sd_before"] == reference.loc[team, "sd_per_week"]


def test_the_after_column_reproduces_simulate_season_on_the_swapped_rosters():
    rosters, schedule = _four_team_league()
    swapped = apply_trade(rosters, "A", "B", ["a1"], ["b1"])
    reference = simulate_season(
        swapped, schedule, _ONE_WR, playoff_start_week=10, end_week=11,
        n=5_000, seed=99,
    ).set_index("team")
    result = evaluate_trade(
        rosters, schedule, _ONE_WR, my_team="A", their_team="B",
        i_give=["a1"], i_get=["b1"], playoff_start_week=10, end_week=11,
        n=5_000, seed=99,
    )
    for team in ("A", "B"):
        row = _row(result, team)
        assert row["championship_prob_after"] == reference.loc[team, "championship_prob"]
        assert row["exp_points_after"] == reference.loc[team, "exp_points_per_week"]


def test_reversing_the_trade_from_the_counterpartys_seat_mirrors_it_exactly():
    rosters, schedule = _four_team_league()
    common = dict(playoff_start_week=10, end_week=11, n=5_000, seed=7)
    mine = evaluate_trade(rosters, schedule, _ONE_WR, my_team="A", their_team="B",
                          i_give=["a1"], i_get=["b1"], **common)
    theirs = evaluate_trade(rosters, schedule, _ONE_WR, my_team="B", their_team="A",
                            i_give=["b1"], i_get=["a1"], **common)
    for team in ("A", "B"):
        left, right = _row(mine, team), _row(theirs, team)
        assert left["delta"] == right["delta"]
        assert left["delta_se"] == right["delta_se"]
        assert left["exp_points_after"] == right["exp_points_after"]
    assert _row(mine, "A")["role"] == "proposer"
    assert _row(theirs, "A")["role"] == "counterparty"
    # Each side's `gives` is what IT sends, not what the proposer sends.
    for result in (mine, theirs):
        assert _row(result, "A")["gives"] == ("a1",)
        assert _row(result, "A")["gets"] == ("b1",)
        assert _row(result, "B")["gives"] == ("b1",)
        assert _row(result, "B")["gets"] == ("a1",)


def test_the_counterparty_re_optimises_its_own_lineup_after_the_swap():
    # B gives away its STARTER (100) and receives a player worse than that
    # starter but better than its own bench (95 > 55). B's post-trade
    # starting points must be 95: not 100 (a lineup that still counts the
    # player it traded away) and not 55 (a lineup that never promotes the
    # incoming player).
    rosters = {
        "A": _roster(("a1", "WR", 95.0, 20.0), ("a2", "WR", 60.0, 20.0)),
        "B": _roster(("b1", "WR", 100.0, 20.0), ("b2", "WR", 55.0, 20.0)),
    }
    schedule = round_robin_schedule(["A", "B"], weeks=3)
    result = _evaluate(rosters, schedule, ["a1"], ["b1"], n=2_000)
    theirs = _row(result, "B")
    assert theirs["exp_points_before"] == 100.0
    assert theirs["exp_points_after"] == 95.0
    assert _row(result, "A")["exp_points_after"] == 100.0


def test_the_playoff_week_win_probability_is_the_one_week_odds_against_the_field():
    # "Playoff-week lineup strength" reported in the unit that decides the
    # title: the chance of winning ONE single-elimination week against an
    # average league opponent, averaged over the other teams.
    rosters, schedule = _four_team_league()
    result = evaluate_trade(
        rosters, schedule, _ONE_WR, my_team="A", their_team="B",
        i_give=["a1"], i_get=["b1"], playoff_start_week=10, end_week=11,
        n=2_000, seed=3,
    )
    mine = _row(result, "A")
    expected_before = np.mean([
        _single_game(90.0, 20.0, 100.0, 22.0),
        _single_game(90.0, 20.0, 85.0, 18.0),
        _single_game(90.0, 20.0, 80.0, 25.0),
    ])
    expected_after = np.mean([
        _single_game(100.0, 22.0, 90.0, 20.0),
        _single_game(100.0, 22.0, 85.0, 18.0),
        _single_game(100.0, 22.0, 80.0, 25.0),
    ])
    assert mine["playoff_win_prob_before"] == pytest.approx(expected_before, rel=1e-12)
    assert mine["playoff_win_prob_after"] == pytest.approx(expected_after, rel=1e-12)
    assert mine["playoff_win_prob_delta"] == pytest.approx(
        expected_after - expected_before, rel=1e-12
    )


def test_a_strict_upgrade_at_the_same_position_never_lowers_my_title_odds():
    # a1 (90) for b1 (100) at the same position with the same spread is a
    # strictly better player. In the two-team closed form the gain is
    # Phi(+d) - Phi(-d); the assertion is that LEVEL, not merely a sign.
    rosters, schedule = _two_team_league()
    result = _evaluate(rosters, schedule, ["a1"], ["b1"])
    mine = _row(result, "A")
    gain = _single_game(100.0, 20.0, 90.0, 20.0) - _single_game(90.0, 20.0, 100.0, 20.0)
    assert mine["delta"] == pytest.approx(gain, abs=0.01)
    assert mine["delta_ci_low"] > 0.0


def test_evaluate_trade_is_deterministic_under_a_fixed_seed():
    rosters, schedule = _four_team_league()
    common = dict(my_team="A", their_team="B", i_give=["a1"], i_get=["b1"],
                  playoff_start_week=10, end_week=11, n=3_000, seed=1234)
    first = evaluate_trade(rosters, schedule, _ONE_WR, **common)
    second = evaluate_trade(rosters, schedule, _ONE_WR, **common)
    pd.testing.assert_frame_equal(first, second)


def test_a_bench_player_adds_nothing_to_the_modelled_lineup_strength():
    # HONEST GAP, PINNED. Bench depth is injury insurance, and this model
    # does not price it: the lineup is set once from expected points and
    # never re-set inside a simulated week, so a fourth WR in a one-WR
    # league is worth EXACTLY zero here even though a real manager values
    # him. `weekly.p_active` is where that would have to enter.
    rosters = {
        "A": _roster(("a1", "WR", 90.0, 20.0), ("a2", "WR", 10.0, 5.0)),
        "B": _roster(("b1", "WR", 100.0, 20.0), ("b2", "WR", 80.0, 20.0)),
    }
    schedule = round_robin_schedule(["A", "B"], weeks=3)
    # A receives an 80-point bench WR for a 10-point bench WR: a large real
    # upgrade in depth, and exactly zero modelled change.
    result = _evaluate(rosters, schedule, ["a2"], ["b2"], n=5_000)
    assert _row(result, "A")["exp_points_delta"] == 0.0
    assert _row(result, "A")["sd_after"] == _row(result, "A")["sd_before"]


def test_evaluate_trade_carries_the_run_settings_in_attrs():
    rosters, schedule = _two_team_league()
    result = _evaluate(rosters, schedule, ["a1"], ["b1"], n=1_000, seed=42)
    assert result.attrs["n"] == 1_000
    assert result.attrs["seed"] == 42
    assert result.attrs["monte_carlo_se"] == pytest.approx(0.5 / math.sqrt(1_000))


# ----------------------------------------------------------------------
# find_trades -- the four-QB pathology, and the search's own guarantees
# ----------------------------------------------------------------------

def _pathology_league() -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """The roster-construction pathology `tt.season` found on real drafted
    rosters, reduced to its smallest honest form.

    One QB slot and two RB slots. Team A hoards FOUR quarterbacks, three of
    whom can never take the field, and starts two replacement-level RBs.
    Team B hoards FOUR running backs and starts a replacement-level QB.
    Neither team is short of talent; both are short of talent THAT CAN
    START, and the same trade fixes both:

      A starts QB 25 + RB 8 + RB 7  = 40
      B starts QB  9 + RB 20 + RB 19 = 48
      C, D start   QB 20 + RB 15 + RB 14 = 49

    A's spare QB is worth 0 to A and 15 to B; B's spare RB is worth 0 to B
    and 12 to A. No positional-need heuristic is needed to see it -- it is
    what `optimal_lineup` already says, which is exactly why this module
    does not add one.
    """
    rosters = {
        "A": _roster(
            ("a_qb1", "QB", 25.0, 8.0), ("a_qb2", "QB", 24.0, 8.0),
            ("a_qb3", "QB", 23.0, 8.0), ("a_qb4", "QB", 22.0, 8.0),
            ("a_rb1", "RB", 8.0, 4.0), ("a_rb2", "RB", 7.0, 4.0),
        ),
        "B": _roster(
            ("b_rb1", "RB", 20.0, 8.0), ("b_rb2", "RB", 19.0, 8.0),
            ("b_rb3", "RB", 18.0, 8.0), ("b_rb4", "RB", 17.0, 8.0),
            ("b_qb1", "QB", 9.0, 4.0), ("b_qb2", "QB", 8.0, 4.0),
        ),
        "C": _roster(
            ("c_qb1", "QB", 20.0, 8.0), ("c_rb1", "RB", 15.0, 6.0),
            ("c_rb2", "RB", 14.0, 6.0), ("c_rb3", "RB", 5.0, 3.0),
            ("c_rb4", "RB", 4.0, 3.0), ("c_qb2", "QB", 6.0, 3.0),
        ),
        "D": _roster(
            ("d_qb1", "QB", 20.0, 8.0), ("d_rb1", "RB", 15.0, 6.0),
            ("d_rb2", "RB", 14.0, 6.0), ("d_rb3", "RB", 5.0, 3.0),
            ("d_rb4", "RB", 4.0, 3.0), ("d_qb2", "QB", 6.0, 3.0),
        ),
    }
    return rosters, round_robin_schedule(list(rosters), weeks=9)


def _position(rosters, team: str, player_id: str) -> str:
    roster = rosters[team]
    return str(roster.loc[roster["player_id"] == player_id, "position"].iloc[0])


def _search(rosters, schedule, **kwargs) -> pd.DataFrame:
    defaults = dict(
        my_team="A", max_give=1, max_get=1,
        playoff_start_week=10, end_week=11, n=8_000, seed=404, screen_top=6,
    )
    defaults.update(kwargs)
    return find_trades(rosters, schedule, _QB_RB, **defaults)


def test_find_trades_finds_a_trade_that_helps_both_sides_of_the_qb_hoard():
    # THE HEADLINE. Mutually beneficial trades are not a theoretical
    # possibility here -- with one QB slot and four QBs on one roster and
    # four RBs on the other, both teams' title odds rise on the same swap.
    rosters, schedule = _pathology_league()
    found = _search(rosters, schedule)
    mutual = found[found["mutual"]]
    assert len(mutual) > 0, "a QB-for-RB swap must help both hoarders"

    best = mutual.iloc[0]
    assert best["their_team"] == "B"
    assert _position(rosters, "A", best["gives"][0]) == "QB"
    assert _position(rosters, "B", best["gets"][0]) == "RB"
    # Levels, not directions: both sides gain a double-digit percentage
    # point of championship probability, and both intervals clear zero.
    assert best["my_delta"] > 0.05
    assert best["their_delta"] > 0.05
    assert best["my_delta_ci_low"] > 0.0
    assert best["their_delta_ci_low"] > 0.0
    # And the mechanism is visible in points, not just in probability.
    assert best["my_exp_points_delta"] > 5.0
    assert best["their_exp_points_delta"] > 5.0
    # Readable both ways round: the names come from the roster that HOLDS
    # each player, so a caller never has to look the ids back up.
    assert best["give_names"] == best["gives"][0].upper()
    assert best["get_names"] == best["gets"][0].upper()


def test_the_best_trade_for_me_gives_away_a_quarterback_that_never_plays():
    # A's second QB contributes exactly nothing to A's lineup, so giving
    # him away must cost A exactly nothing in weekly points -- the trade is
    # a pure gain for A even before the counterparty's side is considered.
    rosters, schedule = _pathology_league()
    best = _search(rosters, schedule).iloc[0]
    assert _position(rosters, "A", best["gives"][0]) == "QB"
    assert best["gives"][0] != "a_qb1", "A must not give away the QB it actually starts"
    assert best["my_exp_points_delta"] > 0.0


def test_find_trades_reports_exactly_the_same_delta_as_evaluate_trade():
    # The two entry points must not be two models. Same seed, same n, same
    # swap -> the same numbers, to the last digit.
    rosters, schedule = _pathology_league()
    found = _search(rosters, schedule)
    top = found.iloc[0]
    one = evaluate_trade(
        rosters, schedule, _QB_RB, my_team="A", their_team=top["their_team"],
        i_give=list(top["gives"]), i_get=list(top["gets"]),
        playoff_start_week=10, end_week=11, n=8_000, seed=404,
    )
    assert top["my_delta"] == _row(one, "A")["delta"]
    assert top["my_delta_se"] == _row(one, "A")["delta_se"]
    assert top["their_delta"] == _row(one, top["their_team"])["delta"]
    assert top["my_exp_points_delta"] == _row(one, "A")["exp_points_delta"]
    assert top["my_championship_prob_after"] == _row(one, "A")["championship_prob_after"]


def test_find_trades_ranks_by_my_delta_descending():
    rosters, schedule = _pathology_league()
    found = _search(rosters, schedule)
    assert len(found) > 3
    assert list(found["my_delta"]) == sorted(found["my_delta"], reverse=True)


def test_mutual_means_both_intervals_clear_zero_not_merely_both_deltas():
    # A trade can raise both point estimates and still be noise on one
    # side. `mutual` is the stricter claim, and the fixture produces rows
    # of both kinds so neither branch is untested.
    rosters, schedule = _pathology_league()
    found = _search(rosters, schedule)
    for _, row in found.iterrows():
        assert bool(row["mutual"]) is bool(
            row["my_delta_ci_low"] > 0.0 and row["their_delta_ci_low"] > 0.0
        )
    assert found["mutual"].any()
    assert not found["mutual"].all()


def test_disabling_the_screen_is_never_worse_than_using_it():
    # The escape hatch that makes the heuristic's cost measurable: with
    # `screen_top=None` every enumerated candidate is simulated, so the
    # exhaustive best is by construction at least as good as the screened
    # best -- and on this fixture the screen happens to lose nothing.
    rosters, schedule = _pathology_league()
    screened = _search(rosters, schedule, screen_top=2)
    exhaustive = _search(rosters, schedule, screen_top=None)
    assert exhaustive.attrs["candidates_simulated"] == exhaustive.attrs["candidates_enumerated"]
    assert exhaustive["my_delta"].max() >= screened["my_delta"].max()
    assert screened.attrs["candidates_simulated"] < exhaustive.attrs["candidates_simulated"]


def test_the_exhaustive_enumeration_is_the_full_combinatorial_product():
    # 6 players each side, packages of up to two, three counterparties:
    # (6 + 15) ** 2 * 3 = 1323 candidate trades. The count is asserted
    # exactly so a silently truncated enumeration cannot pass.
    rosters, schedule = _pathology_league()
    found = _search(rosters, schedule, max_give=2, max_get=2, screen_top=1, n=200)
    assert found.attrs["candidates_enumerated"] == (6 + 15) ** 2 * 3


def test_the_screen_keeps_the_top_of_both_ranking_keys_not_just_one():
    # Stage 1 ranks twice -- by "best for me" and by "best for whichever
    # side does worse" -- and keeps the top `screen_top` of EACH. Against
    # one counterparty with screen_top=2 the two lists overlap in exactly
    # one candidate here, so three trades survive. A screen that kept only
    # one ranking key would simulate two, and a screen that ignored
    # `screen_top` would simulate all 36.
    rosters, schedule = _pathology_league()
    found = _search(rosters, schedule, their_teams=["B"], screen_top=2, n=500)
    assert found.attrs["candidates_enumerated"] == 36
    assert found.attrs["candidates_simulated"] == 3
    assert found.attrs["screen_top"] == 2


def test_the_screen_never_overstates_a_one_for_one_trade():
    # `my_add` measures an arriving player against a roster that still
    # holds the departing one, and lineup value is submodular, so the
    # screen score is a LOWER bound on the true change in weekly starting
    # points for a one-for-one. Asserted for every simulated candidate.
    rosters, schedule = _pathology_league()
    found = _search(rosters, schedule, screen_top=None, n=200)
    assert len(found) == 6 * 6 * 3
    assert (found["screen_score"] <= found["my_exp_points_delta"] + 1e-9).all()
    assert (found["screen_score"] < found["my_exp_points_delta"] - 1e-9).any(), \
        "fixture must contain a candidate the screen genuinely under-rates"


def test_every_candidate_respects_the_package_size_caps():
    rosters, schedule = _pathology_league()
    found = _search(rosters, schedule, max_give=2, max_get=1, screen_top=4, n=500)
    assert {len(row) for row in found["gives"]} <= {1, 2}
    assert {len(row) for row in found["gets"]} == {1}
    assert max(len(row) for row in found["gives"]) == 2


def test_no_candidate_is_a_one_sided_gift():
    # A league that ratifies by vote will never pass a gift, so the search
    # does not spend simulations proposing one.
    rosters, schedule = _pathology_league()
    found = _search(rosters, schedule, max_give=2, max_get=2, screen_top=4, n=500)
    assert all(len(row) >= 1 for row in found["gives"])
    assert all(len(row) >= 1 for row in found["gets"])


def test_find_trades_only_names_players_the_two_teams_actually_hold():
    rosters, schedule = _pathology_league()
    found = _search(rosters, schedule, max_give=2, max_get=2, screen_top=4, n=500)
    for _, row in found.iterrows():
        assert set(row["gives"]) <= set(rosters["A"]["player_id"])
        assert set(row["gets"]) <= set(rosters[row["their_team"]]["player_id"])


def test_find_trades_considers_every_other_team_by_default():
    rosters, schedule = _pathology_league()
    found = _search(rosters, schedule, screen_top=None, n=200)
    assert sorted(found["their_team"].unique()) == ["B", "C", "D"]
    assert found.attrs["their_teams"] == ("B", "C", "D")


def test_their_teams_restricts_the_search_to_the_named_counterparties():
    rosters, schedule = _pathology_league()
    found = _search(rosters, schedule, their_teams=["C"], screen_top=None, n=200)
    assert sorted(found["their_team"].unique()) == ["C"]
    assert found.attrs["candidates_enumerated"] == 6 * 6


def test_find_trades_is_deterministic_under_a_fixed_seed():
    rosters, schedule = _pathology_league()
    first = _search(rosters, schedule, n=2_000)
    second = _search(rosters, schedule, n=2_000)
    pd.testing.assert_frame_equal(first, second)


def test_find_trades_exposes_n_so_accuracy_can_be_traded_for_speed():
    rosters, schedule = _pathology_league()
    found = _search(rosters, schedule, n=1_000)
    assert found.attrs["n"] == 1_000
    assert found.attrs["monte_carlo_se"] == pytest.approx(0.5 / math.sqrt(1_000))
    # A smaller n must widen the paired interval on the same trade.
    coarse = _search(rosters, schedule, n=1_000).iloc[0]
    fine = _search(rosters, schedule, n=16_000).iloc[0]
    assert coarse["my_delta_se"] > fine["my_delta_se"]


def test_find_trades_rejects_an_unknown_team():
    rosters, schedule = _pathology_league()
    # As in apply_trade: an unvalidated lookup raises KeyError("Z") on its
    # own, so the assertion has to be on the message the check produces.
    with pytest.raises(KeyError, match="no roster for team"):
        _search(rosters, schedule, my_team="Z")
    with pytest.raises(KeyError, match=r"no roster for team\(s\)"):
        _search(rosters, schedule, their_teams=["Z"])


def test_find_trades_rejects_trading_with_yourself():
    rosters, schedule = _pathology_league()
    # `apply_trade` would eventually refuse this too, with its own message,
    # so the assertion names the argument `find_trades` is validating -- the
    # only way to tell the two checks apart.
    with pytest.raises(ValueError, match="their_teams names"):
        _search(rosters, schedule, their_teams=["A"])


def test_find_trades_rejects_a_package_size_below_one():
    rosters, schedule = _pathology_league()
    with pytest.raises(ValueError, match="max_give"):
        _search(rosters, schedule, max_give=0)
    with pytest.raises(ValueError, match="screen_top"):
        _search(rosters, schedule, screen_top=0)


def test_a_strictly_better_roster_never_has_lower_title_odds():
    # The one-sided control: A simply RECEIVES b_rb1 (20) and gives up
    # nothing, so its lineup is strictly stronger at the same positions.
    # Championship probability must go UP, and by more than the paired
    # noise -- not merely "not down".
    rosters, schedule = _pathology_league()
    result = evaluate_trade(
        rosters, schedule, _QB_RB, my_team="A", their_team="B",
        i_give=[], i_get=["b_rb1"], playoff_start_week=10, end_week=11,
        n=20_000, seed=77,
    )
    mine = _row(result, "A")
    assert mine["exp_points_delta"] > 0.0
    assert mine["delta"] > 0.0
    assert mine["delta_ci_low"] > 0.0


def test_significance_is_the_two_standard_error_rule_with_both_answers_present():
    # `significant` must be the interval test, not "the delta is nonzero".
    # This fixture produces both kinds of row at n = 2000 -- trades whose
    # measured delta is nonzero but INSIDE the paired noise, and trades
    # well outside it -- so a flag that always fires, never fires, or fires
    # on any nonzero delta all fail here.
    rosters, schedule = _pathology_league()
    found = _search(rosters, schedule, screen_top=None, n=2_000)
    z = 1.959963984540054
    for _, row in found.iterrows():
        assert bool(row["my_significant"]) is bool(
            abs(row["my_delta"]) > z * row["my_delta_se"]
        )
    inside = (found["my_delta"].abs() > 0) & ~found["my_significant"]
    assert inside.any(), "fixture must contain a trade that is real but inside the noise"
    assert found["my_significant"].any()


def test_bare_rosters_are_joined_to_the_weekly_board_and_the_view_is_honoured():
    # The call shape the CLI will actually use: rosters of bare player ids
    # plus a `weekly.project_week` board. The CONDITIONAL / MARGINAL choice
    # is the one thing a caller must not get by accident (`weekly.py` says
    # so, and the two differ by a factor of two for a coin-flip player), so
    # both views are pinned to their exact levels here.
    weekly = pd.DataFrame([
        {"player_id": "a1", "name": "A1", "position": "WR",
         "exp_points": 90.0, "sd": 20.0, "exp_points_marginal": 45.0, "sd_marginal": 33.0},
        {"player_id": "b1", "name": "B1", "position": "WR",
         "exp_points": 100.0, "sd": 20.0, "exp_points_marginal": 95.0, "sd_marginal": 24.0},
    ])
    rosters = {
        "A": pd.DataFrame([{"player_id": "a1"}]),
        "B": pd.DataFrame([{"player_id": "b1"}]),
    }
    schedule = round_robin_schedule(["A", "B"], weeks=3)
    common = dict(
        my_team="A", their_team="B", i_give=["a1"], i_get=["b1"],
        weekly=weekly, playoff_start_week=4, end_week=4, n=1_000, seed=8,
    )
    marginal = evaluate_trade(rosters, schedule, _ONE_WR, marginal=True, **common)
    conditional = evaluate_trade(rosters, schedule, _ONE_WR, marginal=False, **common)

    assert _row(marginal, "A")["exp_points_before"] == 45.0
    assert _row(marginal, "A")["sd_before"] == 33.0
    assert _row(marginal, "A")["exp_points_after"] == 95.0
    assert _row(conditional, "A")["exp_points_before"] == 90.0
    assert _row(conditional, "A")["sd_before"] == 20.0
    assert _row(conditional, "A")["exp_points_after"] == 100.0


def _thin_edge_league() -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """The same roster-construction inefficiency as `_pathology_league`,
    shrunk until the gains are worth tenths of a point instead of tens.
    A's spare QB and B's third RB still help each other -- but by so little
    that at n = 4000 the paired interval on the title probability covers
    zero. This is the regime `mutual` exists to be honest about.
    """
    rosters = {
        "A": _roster(("a_qb1", "QB", 20.0, 8.0), ("a_qb2", "QB", 19.0, 8.0),
                     ("a_rb1", "RB", 12.0, 5.0), ("a_rb2", "RB", 11.0, 5.0)),
        "B": _roster(("b_rb1", "RB", 13.0, 5.0), ("b_rb2", "RB", 12.5, 5.0),
                     ("b_rb3", "RB", 12.4, 5.0), ("b_qb1", "QB", 19.0, 8.0)),
        "C": _roster(("c_qb1", "QB", 20.0, 8.0), ("c_rb1", "RB", 12.5, 5.0),
                     ("c_rb2", "RB", 12.0, 5.0), ("c_rb3", "RB", 6.0, 3.0)),
        "D": _roster(("d_qb1", "QB", 20.0, 8.0), ("d_rb1", "RB", 12.4, 5.0),
                     ("d_rb2", "RB", 11.9, 5.0), ("d_rb3", "RB", 6.0, 3.0)),
    }
    return rosters, round_robin_schedule(list(rosters), weeks=9)


def test_mutual_is_not_satisfied_by_two_positive_deltas_inside_the_noise():
    # `mutual` is the claim that BOTH sides are measurably better off, not
    # that both point estimates happen to be positive. On this fixture
    # several candidates raise both teams' measured title probability while
    # at least one paired interval still covers zero -- and every one of
    # them must be reported as NOT mutual.
    rosters, schedule = _thin_edge_league()
    found = _search(rosters, schedule, screen_top=None, n=4_000)
    both_positive = (found["my_delta"] > 0) & (found["their_delta"] > 0)
    assert both_positive.sum() >= 2, "fixture must produce both-positive candidates"
    assert not found.loc[both_positive, "mutual"].any()
    for _, row in found.iterrows():
        assert bool(row["mutual"]) is bool(
            row["my_delta_ci_low"] > 0.0 and row["their_delta_ci_low"] > 0.0
        )


def _interacting_package_league() -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """A league built to expose the screen's WORST failure: a package trade
    whose parts interact.

    One QB slot, two RB slots. A starts QB 20 + RB 15 + RB 10 = 45; B starts
    QB 19 + RB 16 + RB 14 = 49. The trade that helps both is A sending its
    RB1 (15) and a spare QB for B's RB2 (14) and RB3 (13): A ends up starting
    14 + 13 = 27 instead of 15 + 10 = 25 (+2), and B's lost RB2 is replaced
    by the arriving RB1, so B starts 19 + 16 + 15 = 50 instead of 49 (+1).

    The additive screen cannot see it. It charges A the FULL 11 points of
    giving up RB1 while crediting the two arriving backs only against a
    roster that still holds RB1 (4 and 3), scoring the trade at MINUS 4 when
    it is worth PLUS 2 -- and it charges B for losing RB2 without noticing
    that the incoming RB1 replaces him.
    """
    rosters = {
        "A": _roster(("a_qb1", "QB", 20.0, 8.0), ("a_rb1", "RB", 15.0, 6.0),
                     ("a_rb2", "RB", 10.0, 6.0), ("a_qb2", "QB", 12.0, 8.0),
                     ("a_rb3", "RB", 4.0, 3.0)),
        "B": _roster(("b_qb1", "QB", 19.0, 8.0), ("b_rb1", "RB", 16.0, 6.0),
                     ("b_rb2", "RB", 14.0, 6.0), ("b_rb3", "RB", 13.0, 6.0),
                     ("b_qb2", "QB", 11.0, 8.0)),
        "C": _roster(("c_qb1", "QB", 19.5, 8.0), ("c_rb1", "RB", 15.0, 6.0),
                     ("c_rb2", "RB", 14.0, 6.0), ("c_rb3", "RB", 5.0, 3.0),
                     ("c_qb2", "QB", 10.0, 8.0)),
        "D": _roster(("d_qb1", "QB", 19.5, 8.0), ("d_rb1", "RB", 15.0, 6.0),
                     ("d_rb2", "RB", 14.0, 6.0), ("d_rb3", "RB", 5.0, 3.0),
                     ("d_qb2", "QB", 10.0, 8.0)),
    }
    return rosters, round_robin_schedule(list(rosters), weeks=9)


def test_the_screen_can_miss_a_mutually_beneficial_package_that_the_exhaustive_search_finds():
    # THE HEURISTIC'S COST, PINNED RATHER THAN ASSERTED AWAY. This is not a
    # hypothetical weakness: on this project's real drafted rosters the
    # screened 2-for-2 search reports zero mutually beneficial trades where
    # an exhaustive one finds 49. The fixture reproduces the mechanism at a
    # size a test can assert exactly.
    rosters, schedule = _interacting_package_league()
    common = dict(my_team="A", max_give=2, max_get=2, their_teams=["B"],
                  playoff_start_week=10, end_week=11, n=8_000, seed=404)
    screened = find_trades(rosters, schedule, _QB_RB, screen_top=4, **common)
    exhaustive = find_trades(rosters, schedule, _QB_RB, screen_top=None, **common)

    assert screened.attrs["candidates_simulated"] == 8
    assert exhaustive.attrs["candidates_simulated"] == 225
    # The screen finds NONE of them; the exhaustive search finds them.
    assert int(screened["mutual"].sum()) == 0
    assert int(exhaustive["mutual"].sum()) == 8

    best = exhaustive[exhaustive["mutual"]].iloc[0]
    # Levels: +2.0 and +1.0 points a week, exactly as the fixture is built.
    assert best["my_exp_points_delta"] == 2.0
    assert best["their_exp_points_delta"] == 1.0
    assert best["my_delta_ci_low"] > 0.0
    assert best["their_delta_ci_low"] > 0.0
    # And the reason it was missed: the screen scored a +2.0-point trade
    # NEGATIVE, which is the lower-bound property being very loose rather
    # than the bound being violated.
    assert best["screen_score"] < 0.0
    assert best["screen_score"] < best["my_exp_points_delta"]
