"""The playoff (variance-aware) lineup optimiser (docs/draft-engine-design.md
3.6, second half): maximise `P(my total > their total)`, NOT expected
points.

WHY THIS MODULE IS DIFFERENT. Every other optimiser in this engine
maximises expected points. In a one-game elimination match, expected
margin is irrelevant -- only win probability counts, and it inverts on
context: a heavy underdog's median outcome loses anyway, so a high-variance
player is strictly better (only the tail outcomes win); a heavy favourite's
median outcome wins, so variance only creates ways to lose and a
high-floor player is strictly better. `tt.playoff` has to reproduce BOTH
behaviours from ONE objective -- `win_probability` -- with no
`if underdog: ...` branch anywhere. The starkest proof available is
structural: `playoff_lineup`'s objective function, `win_probability`, is
the only place `their_lineup` is even consulted, and `tt.lineup.
optimal_lineup` -- the pure expected-points optimiser this module reuses
for legality and as a comparison baseline -- has NO opponent parameter AT
ALL. It is therefore *impossible* for `optimal_lineup` to depend on who
you're playing, which is exactly why it cannot reproduce the inversion and
`playoff.py` has to exist.

THE GAUSSIAN-INDEPENDENT MODEL, AND WHY THE TEST NUMBERS BELOW ARE EXACT,
NOT APPROXIMATE. `win_probability` samples each starter's score from
Normal(proj_points, sd), independently, and sums. Under that model a whole
lineup's total is itself Normal(sum of means, sum of variances) (closed
under summation), so `P(my > their)` has a closed form,
`Phi((my_mean - their_mean) / sqrt(my_var + their_var))`, that every
hand-computed expectation below is checked against via `scipy.stats.norm`.
Crucially, whenever one side's sd is exactly 0.0, that side's Monte Carlo
draws are NOT samples at all -- `rng.normal(loc=mu, scale=0.0, ...)`
degenerates to the constant `mu` on every draw (verified empirically before
writing these tests) -- so any comparison against a sd=0.0 lineup returns
EXACTLY 0.0 or EXACTLY 1.0, with zero sampling error, regardless of seed or
n. Every test that needs a hard, non-flaky boundary (the inversion test
included) is deliberately built on that fact rather than on "close to X".
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from tt.league import load_config_from_dict
from tt.lineup import optimal_lineup
from tt.playoff import playoff_lineup, win_probability

# A minimal one-slot, one-position league: isolates a single roster
# decision (who starts at the one WR slot) from every other slot's
# bookkeeping, the same "keep the fixture hand-computable" approach
# test_lineup.py uses for its own flex-stranding test.
_ONE_WR_CONFIG = load_config_from_dict({
    "leagueKey": "x", "name": "x", "numTeams": 10, "maxTeams": 10,
    "draftStatus": "predraft", "rosterSlots": {"WR": 1}, "scoring": [],
})

_ONE_RB_CONFIG = load_config_from_dict({
    "leagueKey": "x", "name": "x", "numTeams": 10, "maxTeams": 10,
    "draftStatus": "predraft", "rosterSlots": {"RB": 1}, "scoring": [],
})

# The real league's shape (see test_mock.py / test_lineup.py's own
# _REAL_SHAPE_CONFIG): enough slots and flex to exercise the swap search
# for real, not just a single-slot toy.
_REAL_SHAPE_CONFIG = load_config_from_dict({
    "leagueKey": "x", "name": "x", "numTeams": 10, "maxTeams": 10,
    "draftStatus": "predraft",
    "rosterSlots": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1},
    "scoring": [],
})


def _analytic_p_win(my_mean, my_sd, their_mean, their_sd):
    total_sd = math.sqrt(my_sd**2 + their_sd**2)
    if total_sd == 0:
        return 1.0 if my_mean > their_mean else 0.0
    return float(norm.cdf((my_mean - their_mean) / total_sd))


# ---------------------------------------------------------------------------
# 2. A hand-computable win_probability: sd=0 lineups give exactly 0.0 or 1.0.
# ---------------------------------------------------------------------------

def test_win_probability_is_exactly_one_when_deterministically_ahead():
    mine = pd.DataFrame([{"proj_points": 50.0, "sd": 0.0}])
    theirs = pd.DataFrame([{"proj_points": 40.0, "sd": 0.0}])
    assert win_probability(mine, theirs, n=100, seed=1) == 1.0


def test_win_probability_is_exactly_zero_when_deterministically_behind():
    mine = pd.DataFrame([{"proj_points": 40.0, "sd": 0.0}])
    theirs = pd.DataFrame([{"proj_points": 50.0, "sd": 0.0}])
    assert win_probability(mine, theirs, n=100, seed=1) == 0.0


def test_win_probability_ties_are_not_counted_as_a_win():
    # DECISION (documented in playoff.py): P(my > their) is a STRICT
    # inequality. An exact tie -- only reachable in practice when both
    # sides are deterministic (sd=0) and land on the same number -- counts
    # as a loss, not a win and not a 0.5 coin flip. This pins that choice.
    mine = pd.DataFrame([{"proj_points": 40.0, "sd": 0.0}])
    theirs = pd.DataFrame([{"proj_points": 40.0, "sd": 0.0}])
    assert win_probability(mine, theirs, n=100, seed=1) == 0.0


def test_win_probability_sums_multiple_starters_before_comparing():
    # Two 20-point (sd=0) starters vs one deterministic 45-point opponent
    # total: 40 < 45, so I lose -- proves the lineup is SUMMED, not
    # compared player-by-player.
    mine = pd.DataFrame([
        {"proj_points": 20.0, "sd": 0.0},
        {"proj_points": 20.0, "sd": 0.0},
    ])
    theirs = pd.DataFrame([{"proj_points": 45.0, "sd": 0.0}])
    assert win_probability(mine, theirs, n=100, seed=1) == 0.0


def test_win_probability_matches_the_closed_form_gaussian_sum():
    # Not a sd=0 case -- checks the actual Monte Carlo estimate against the
    # closed-form Normal-sum answer (see module docstring) at a generous n,
    # so this pins the model itself (independent Normal per starter,
    # summed), not just its two degenerate corners.
    mine = pd.DataFrame([
        {"proj_points": 20.0, "sd": 5.0},
        {"proj_points": 15.0, "sd": 3.0},
    ])
    theirs = pd.DataFrame([{"proj_points": 30.0, "sd": 6.0}])
    got = win_probability(mine, theirs, n=200_000, seed=1)
    expected = _analytic_p_win(35.0, math.sqrt(5.0**2 + 3.0**2), 30.0, 6.0)
    assert got == pytest.approx(expected, abs=0.01)


# ---------------------------------------------------------------------------
# 3. Seeded reproducibility.
# ---------------------------------------------------------------------------

def test_win_probability_is_reproducible_for_a_fixed_seed():
    mine = pd.DataFrame([{"proj_points": 20.0, "sd": 8.0}])
    theirs = pd.DataFrame([{"proj_points": 18.0, "sd": 8.0}])
    a = win_probability(mine, theirs, n=5000, seed=42)
    b = win_probability(mine, theirs, n=5000, seed=42)
    assert a == b


def test_win_probability_differs_across_seeds_when_variance_is_nonzero():
    # The flip side of reproducibility: it must actually be a genuine Monte
    # Carlo estimate (finite-n sampling noise), not a seed-blind closed-form
    # shortcut wearing a `seed` parameter it ignores.
    mine = pd.DataFrame([{"proj_points": 20.0, "sd": 8.0}])
    theirs = pd.DataFrame([{"proj_points": 18.0, "sd": 8.0}])
    values = {win_probability(mine, theirs, n=200, seed=s) for s in range(10)}
    assert len(values) > 1


def test_playoff_lineup_is_reproducible_for_a_fixed_seed():
    roster = pd.DataFrame([
        {"player_id": "steady", "name": "Steady", "position": "WR", "proj_points": 20.0, "sd": 0.0},
        {"player_id": "boom_bust", "name": "Boom", "position": "WR", "proj_points": 20.0, "sd": 80.0},
    ])
    their_lineup = pd.DataFrame([{"player_id": "opp", "proj_points": 100.0, "sd": 0.0}])
    a = playoff_lineup(roster, their_lineup, _ONE_WR_CONFIG, n=5000, seed=7)
    b = playoff_lineup(roster, their_lineup, _ONE_WR_CONFIG, n=5000, seed=7)
    a_starters = a[a["starter"]]["player_id"].tolist()
    b_starters = b[b["starter"]]["player_id"].tolist()
    assert a_starters == b_starters
    assert a.attrs["win_probability"] == b.attrs["win_probability"]


# ---------------------------------------------------------------------------
# 1. THE INVERSION -- the module's reason to exist.
#
# One WR slot, two candidates of EQUAL expected points (20.0): "steady"
# (sd=0.0) and "boom_bust" (sd=80.0). Because both sides of the swap are
# deterministic on the STEADY branch (sd=0.0 lineup vs sd=0.0 opponent),
# win_probability there is exactly 0.0 or exactly 1.0 with NO sampling
# noise -- so whichever way the boom_bust MC estimate lands, the
# comparison against steady's hard 0.0/1.0 boundary can never be
# ambiguous, at any seed, at any n >= a few hundred. See module docstring.
# ---------------------------------------------------------------------------

_STEADY_VS_BOOM_ROSTER = pd.DataFrame([
    # steady listed FIRST -- optimal_lineup's stable-sort tiebreak (see
    # tt.lineup module docstring, "DETERMINISM") means a tie in proj_points
    # always resolves to whichever player appears first in the input,
    # regardless of any opponent -- which is exactly the point: EP-only
    # optimisation is blind to who you're playing.
    {"player_id": "steady", "name": "Steady", "position": "WR", "proj_points": 20.0, "sd": 0.0},
    {"player_id": "boom_bust", "name": "Boom", "position": "WR", "proj_points": 20.0, "sd": 80.0},
])


def test_expected_points_optimum_cannot_see_the_opponent_at_all():
    # optimal_lineup doesn't even TAKE an opponent argument -- so a single
    # call already proves its answer can never change with who you play.
    # Both players are tied at 20.0 proj_points, so the stable tiebreak
    # always picks "steady" (input order), independent of any opponent.
    lineup = optimal_lineup(_STEADY_VS_BOOM_ROSTER, _ONE_WR_CONFIG)
    starter = lineup[lineup["starter"]].iloc[0]
    assert starter["player_id"] == "steady"


def test_playoff_lineup_prefers_boom_bust_as_a_heavy_underdog():
    # Analytic P(win): steady vs opp(100, sd=0) = 0.0 exactly;
    # boom_bust vs the same opponent = Phi((20-100)/80) = Phi(-1.0) ~ 0.159.
    strong_opponent = pd.DataFrame([{"player_id": "opp", "proj_points": 100.0, "sd": 0.0}])
    result = playoff_lineup(_STEADY_VS_BOOM_ROSTER, strong_opponent, _ONE_WR_CONFIG, n=20_000, seed=1)
    starter = result[result["starter"]].iloc[0]
    assert starter["player_id"] == "boom_bust"
    assert result.attrs["win_probability"] > 0.0
    # And the win probability actually achieved is close to the closed form.
    expected = _analytic_p_win(20.0, 80.0, 100.0, 0.0)
    assert result.attrs["win_probability"] == pytest.approx(expected, abs=0.02)


def test_playoff_lineup_prefers_steady_as_a_heavy_favourite():
    # Analytic P(win): steady vs opp(5, sd=0) = 1.0 exactly;
    # boom_bust vs the same opponent = Phi((20-5)/80) = Phi(0.1875) ~ 0.574.
    weak_opponent = pd.DataFrame([{"player_id": "opp", "proj_points": 5.0, "sd": 0.0}])
    result = playoff_lineup(_STEADY_VS_BOOM_ROSTER, weak_opponent, _ONE_WR_CONFIG, n=20_000, seed=1)
    starter = result[result["starter"]].iloc[0]
    assert starter["player_id"] == "steady"
    assert result.attrs["win_probability"] == pytest.approx(1.0)


def test_the_same_roster_and_function_invert_purely_on_the_opponent_argument():
    # The single strongest statement of the module's reason to exist: ONE
    # roster, ONE function (`playoff_lineup`), no opponent-conditional code
    # path anywhere in its call -- yet the chosen starter flips depending
    # solely on the `their_lineup` argument.
    strong_opponent = pd.DataFrame([{"player_id": "opp", "proj_points": 100.0, "sd": 0.0}])
    weak_opponent = pd.DataFrame([{"player_id": "opp", "proj_points": 5.0, "sd": 0.0}])
    vs_strong = playoff_lineup(_STEADY_VS_BOOM_ROSTER, strong_opponent, _ONE_WR_CONFIG, n=20_000, seed=3)
    vs_weak = playoff_lineup(_STEADY_VS_BOOM_ROSTER, weak_opponent, _ONE_WR_CONFIG, n=20_000, seed=3)
    starter_vs_strong = vs_strong[vs_strong["starter"]].iloc[0]["player_id"]
    starter_vs_weak = vs_weak[vs_weak["starter"]].iloc[0]["player_id"]
    assert starter_vs_strong == "boom_bust"
    assert starter_vs_weak == "steady"
    assert starter_vs_strong != starter_vs_weak


# ---------------------------------------------------------------------------
# 4. Evenly matched opponent: the objectives converge.
#
# Four WR candidates with DIFFERENT means and different (but not equal） sds,
# an opponent whose lineup total sits at exact parity with the top-2-by-
# mean combination. Every OTHER combination of 2 has strictly lower
# analytic win probability than the parity combination's 0.5 (verified by
# hand below and cross-checked against scipy at module-authoring time) --
# so if playoff_lineup's swap search ever wandered off the expected-points
# lineup here, it would be moving to something demonstrably worse, not
# equally good.
# ---------------------------------------------------------------------------

_PARITY_ROSTER = pd.DataFrame([
    {"player_id": "wr1", "position": "WR", "proj_points": 15.0, "sd": 3.0},
    {"player_id": "wr2", "position": "WR", "proj_points": 12.0, "sd": 6.0},
    {"player_id": "wr3", "position": "WR", "proj_points": 9.0, "sd": 3.0},
    {"player_id": "wr4", "position": "WR", "proj_points": 6.0, "sd": 6.0},
])
_PARITY_CONFIG = load_config_from_dict({
    "leagueKey": "x", "name": "x", "numTeams": 10, "maxTeams": 10,
    "draftStatus": "predraft", "rosterSlots": {"WR": 2}, "scoring": [],
})
_PARITY_OPPONENT = pd.DataFrame([{"player_id": "opp", "proj_points": 27.0, "sd": 6.0}])


def test_evenly_matched_opponent_converges_to_the_expected_points_lineup():
    ep_lineup = optimal_lineup(_PARITY_ROSTER, _PARITY_CONFIG)
    ep_starters = set(ep_lineup[ep_lineup["starter"]]["player_id"])
    assert ep_starters == {"wr1", "wr2"}  # top-2 by proj_points

    result = playoff_lineup(_PARITY_ROSTER, _PARITY_OPPONENT, _PARITY_CONFIG, n=20_000, seed=9)
    playoff_starters = set(result[result["starter"]]["player_id"])
    assert playoff_starters == ep_starters
    assert result.attrs["win_probability"] == pytest.approx(0.5, abs=0.02)
    assert result.attrs["win_probability"] == pytest.approx(
        result.attrs["expected_points_lineup_win_probability"], abs=1e-9
    )


# ---------------------------------------------------------------------------
# Structure / plumbing: playoff_lineup produces a legal lineup shape and
# reports the comparison data the docstring promises.
# ---------------------------------------------------------------------------

def test_playoff_lineup_is_legal_and_reports_comparison_attrs():
    roster = pd.DataFrame([
        {"player_id": "qb1", "name": "QB1", "position": "QB", "proj_points": 18.0, "sd": 4.0},
        {"player_id": "rb1", "name": "RB1", "position": "RB", "proj_points": 14.0, "sd": 5.0},
        {"player_id": "rb2", "name": "RB2", "position": "RB", "proj_points": 11.0, "sd": 4.0},
        {"player_id": "rb3", "name": "RB3", "position": "RB", "proj_points": 9.0, "sd": 10.0},
        {"player_id": "wr1", "name": "WR1", "position": "WR", "proj_points": 13.0, "sd": 4.0},
        {"player_id": "wr2", "name": "WR2", "position": "WR", "proj_points": 10.0, "sd": 4.0},
        {"player_id": "wr3", "name": "WR3", "position": "WR", "proj_points": 7.0, "sd": 6.0},
        {"player_id": "te1", "name": "TE1", "position": "TE", "proj_points": 8.0, "sd": 3.0},
        {"player_id": "k1", "name": "K1", "position": "K", "proj_points": 7.0, "sd": 2.0},
        {"player_id": "def1", "name": "DEF1", "position": "DEF", "proj_points": 6.0, "sd": 3.0},
    ])
    their_lineup = pd.DataFrame([{"player_id": "opp", "proj_points": 80.0, "sd": 15.0}])

    result = playoff_lineup(roster, their_lineup, _REAL_SHAPE_CONFIG, n=3000, seed=11)

    starters = result[result["starter"]]
    # Exactly one row per configured starting slot (mirrors optimal_lineup's
    # own contract -- see tt.lineup module docstring, "THIN ROSTERS").
    assert len(starters) == sum(_REAL_SHAPE_CONFIG.roster_slots.values())
    assert set(starters["slot"]) == set(_REAL_SHAPE_CONFIG.roster_slots)
    # No player starts twice.
    ids = starters["player_id"].dropna().tolist()
    assert len(ids) == len(set(ids))

    assert 0.0 <= result.attrs["win_probability"] <= 1.0
    assert "expected_points_lineup_win_probability" in result.attrs
    assert "expected_points_lineup_points" in result.attrs
    assert "playoff_lineup_points" in result.attrs
    assert result.attrs["expected_points_lineup_points"] > 0


def test_playoff_lineup_never_starts_a_bench_only_player_twice_and_never_drops_the_config_slot_count():
    # Regression against a sloppy swap implementation that could duplicate
    # a player across two slots or lose a slot while shuffling starters
    # in/out of the bench pool.
    result = playoff_lineup(
        _STEADY_VS_BOOM_ROSTER,
        pd.DataFrame([{"player_id": "opp", "proj_points": 100.0, "sd": 0.0}]),
        _ONE_WR_CONFIG, n=2000, seed=2,
    )
    assert len(result) == 2  # 1 starting slot + 1 bench player, always
    assert set(result["player_id"]) == {"steady", "boom_bust"}


def test_a_displaced_starter_moves_to_the_bench_instead_of_staying_a_starter():
    """A swap must REPLACE a starter, not add one.

    The hill-climb builds each trial lineup by overwriting `starters[i]`
    with the bench candidate, then pushes the displaced player onto the
    bench list -- but the displaced dict still carried the `starter=True`
    and `slot` it held while starting. Concatenating starters and bench
    then produced a frame with MORE rows flagged `starter` than the league
    has slots, i.e. an illegal lineup that `win_probability` (which selects
    on `starter & ~empty`) would happily score as if you could field two
    players in one slot.

    Asserts the LEVEL -- exactly one starter for a one-slot league -- not
    merely that a swap happened.
    """
    roster = pd.DataFrame({
        "player_id": ["steady", "boom"],
        "name": ["Steady RB", "Boom RB"],
        "position": ["RB", "RB"],
        "proj_points": [20.0, 19.0],
        "sd": [3.0, 12.0],
    })
    # A far-ahead opponent: only the high-variance tail wins, so the
    # optimiser is forced to swap and therefore to displace someone.
    their = pd.DataFrame({
        "player_id": ["opp"], "name": ["Opponent"], "position": ["RB"],
        "proj_points": [60.0], "sd": [1.0],
    })
    out = playoff_lineup(roster, their, _ONE_RB_CONFIG, n=4000, seed=11)

    live = out[out["starter"].fillna(False).astype(bool)]
    live = live[~live["empty"].fillna(False).astype(bool)]
    assert len(live) == 1, f"one RB slot must yield one starter, got {len(live)}"
    assert live.iloc[0]["player_id"] == "boom"

    displaced = out[out["player_id"] == "steady"].iloc[0]
    assert bool(displaced["starter"]) is False
    assert displaced["slot"] is None or pd.isna(displaced["slot"])
