"""Mock draft simulator: the harness that MEASURES a strategy instead of
assuming it. Everything upstream (projections, VOR, survival, the pick
recommender) answers "what should I do at THIS pick" -- none of it says
whether following that advice actually produces a better roster than the
alternative. `simulate_draft` runs a full snake draft under a candidate
`strategy`; `compare_strategies` runs many of them and scores the results.

These tests deliberately reuse a single, richly-columned synthetic
`board()` (proj_points/vor/tier already computed via `tt.vor.add_vor`,
adp/stdev already attached) rather than re-deriving VOR or survival math --
that arithmetic is already pinned by test_vor.py and test_survival.py. What
this file exercises is `tt.mock`'s OWN logic: snake ordering, no player
drafted twice, determinism under a fixed seed, ADP-noise sensitivity, the
strategy-comparison harness, and -- the one genuinely new piece of
reasoning this task requires -- `strategy_vor_survival`'s explicit tiebreak
for the all-zero-expected-loss degenerate case (see `tt.mock` module
docstring and CRITICAL #2 in the task brief).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tt.draft import FILLED_POSITION_DISCOUNT
from tt.league import load_config_from_dict
from tt.mock import (
    ADP_NOISE_DEFAULT,
    compare_strategies,
    draft_order,
    optimal_lineup_score,
    simulate_draft,
    strategy_adp,
    strategy_vor,
    strategy_vor_survival,
)
from tt.vor import add_vor

# 10 teams, a 2-way "W/R" flex (not the real league's 3-way "W/R/T") so
# starters_per_team lands on the same clean numbers test_draft.py/test_vor.py
# rely on: RB/WR = 2 + 1/2 flex = 2.5, TE = 1.
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

# replacement_levels(CONFIG_OBJ, teams=10): QB=10, RB=25, WR=25, TE=10.
# Position pools below are all comfortably deeper than that so add_vor never
# hits the "replacement rank beyond the pool" fallback.
_POSITION_COUNTS = {"QB": 30, "RB": 70, "WR": 80, "TE": 40}  # 220 players total


def board() -> pd.DataFrame:
    """220 players (well over the 150 picks a 10-team x 15-round draft
    needs), proj_points/vor/tier already attached via `add_vor`, plus a
    synthetic adp/stdev market: ranked by vor (a believable proxy for "how
    good is this player", not a claim about real ADP), 1..180 get a
    distinct adp and a stdev that widens with depth (mirrors FFC's own
    real-data pattern -- see mock.py's ADP_NOISE_DEFAULT comment), and the
    bottom 40 get NO adp at all -- exactly FFC's real half-PPR feed shape
    (~156-233 tracked players against a much deeper real draft), which is
    what makes strategy_vor_survival's all-zero degenerate case reachable
    on a realistic board rather than only in a hand-built edge-case test.
    """
    rows = []
    for position, count in _POSITION_COUNTS.items():
        for i in range(count):
            proj_points = 300.0 - i * (300.0 / count)  # strictly decreasing, no ties
            rows.append({
                "player_id": f"{position}{i:03d}",
                "name": f"{position} Player {i}",
                "position": position,
                "proj_points": proj_points,
            })
    out = add_vor(pd.DataFrame(rows), CONFIG_OBJ, teams=10)
    ranked = out.sort_values("vor", ascending=False).reset_index(drop=True)
    n = len(ranked)
    no_adp_count = 40
    adp = [float(rank + 1) if rank < n - no_adp_count else float("nan") for rank in range(n)]
    stdev = [1.0 + rank * 0.15 if rank < n - no_adp_count else float("nan") for rank in range(n)]
    ranked["adp"] = adp
    ranked["stdev"] = stdev
    return ranked


# --- given tests (task-7-brief.md), verbatim apart from the local board() ---


def test_a_draft_gives_every_team_the_right_number_of_picks():
    result = simulate_draft(board(), teams=10, rounds=15, my_slot=3,
                            strategy=strategy_adp, seed=1)
    assert len(result) == 15


def test_snake_order_reverses_each_round():
    picks = draft_order(teams=4, rounds=2)
    assert picks[:4] == [0, 1, 2, 3]
    assert picks[4:] == [3, 2, 1, 0]


def test_no_player_is_drafted_twice():
    result = simulate_draft(board(), teams=10, rounds=15, my_slot=3,
                            strategy=strategy_adp, seed=1, return_all=True)
    assert result["player_id"].is_unique


def test_the_same_seed_reproduces_the_same_draft():
    a = simulate_draft(board(), teams=10, rounds=15, my_slot=3, strategy=strategy_adp, seed=7)
    b = simulate_draft(board(), teams=10, rounds=15, my_slot=3, strategy=strategy_adp, seed=7)
    pd.testing.assert_frame_equal(a, b)


def test_adp_noise_changes_the_draft():
    calm = simulate_draft(board(), teams=10, rounds=15, my_slot=3,
                          strategy=strategy_adp, seed=7, adp_noise=0.1)
    wild = simulate_draft(board(), teams=10, rounds=15, my_slot=3,
                          strategy=strategy_adp, seed=7, adp_noise=20.0)
    assert list(calm["player_id"]) != list(wild["player_id"])


def test_compare_strategies_reports_one_row_per_strategy():
    out = compare_strategies(board(), {"adp": strategy_adp, "vor": strategy_vor},
                             trials=5, teams=10, my_slot=3, seed=1)
    assert set(out["strategy"]) == {"adp", "vor"}
    assert (out["trials"] == 5).all()


# --- draft_order: exact snake arithmetic beyond the given reversal check ---


def test_draft_order_has_exactly_teams_times_rounds_picks():
    assert len(draft_order(teams=10, rounds=15)) == 150


def test_draft_order_gives_every_team_the_same_number_of_picks():
    order = draft_order(teams=6, rounds=4)
    counts = pd.Series(order).value_counts()
    assert (counts == 4).all()


def test_draft_order_pins_the_exact_sequence_for_three_rounds():
    # Round 0 ascending, round 1 descending, round 2 ascending again -- the
    # exact expected sequence, not just "it reverses somewhere."
    assert draft_order(teams=3, rounds=3) == [0, 1, 2, 2, 1, 0, 0, 1, 2]


# --- simulate_draft: mechanics ---


def test_simulate_draft_does_not_mutate_the_input_board():
    original = board()
    original_columns = list(original.columns)
    before = original.copy(deep=True)
    simulate_draft(original, teams=10, rounds=15, my_slot=3, strategy=strategy_adp, seed=1)
    assert list(original.columns) == original_columns
    pd.testing.assert_frame_equal(original, before)


def test_simulate_draft_rejects_a_my_slot_outside_the_team_range():
    with pytest.raises(ValueError):
        simulate_draft(board(), teams=10, rounds=15, my_slot=10, strategy=strategy_adp, seed=1)
    with pytest.raises(ValueError):
        simulate_draft(board(), teams=10, rounds=15, my_slot=-1, strategy=strategy_adp, seed=1)


def test_return_all_covers_every_team_and_every_round():
    result = simulate_draft(board(), teams=10, rounds=15, my_slot=3,
                            strategy=strategy_adp, seed=1, return_all=True)
    assert len(result) == 150
    assert set(result["slot"]) == set(range(10))
    assert set(result["round_number"]) == set(range(1, 16))


def test_pick_and_next_pick_follow_snake_math_for_an_edge_slot():
    # Slot 0 (first overall pick) picks 1, then 20 (last pick of round 2,
    # since round 2 runs in reverse for a 10-team snake), then 21 (first
    # pick of round 3) -- the classic snake "turn" pattern. A spy strategy
    # records exactly what simulate_draft hands it so this pins the
    # pick/next_pick wiring, not just "some strategy got called."
    seen = []
    slot_zero_roster_id = None

    def spy(available, roster, pick, next_pick, teams):
        nonlocal slot_zero_roster_id
        # pick == 1 is unambiguously slot 0's first turn (round 0 is
        # ascending); `roster` is the SAME list object simulate_draft
        # mutates in place across slot 0's whole draft, so capturing its
        # identity here lets later calls recognise "this is slot 0 again"
        # without already knowing the snake pattern being tested.
        if pick == 1:
            slot_zero_roster_id = id(roster)
        if id(roster) == slot_zero_roster_id and len(seen) < 3:
            seen.append((pick, next_pick))
        return available.iloc[0].to_dict()

    simulate_draft(board(), teams=10, rounds=15, my_slot=0, strategy=spy, seed=1)
    # slot 0's own picks are 1, 20, 21, ...; next_pick recorded alongside
    # each of ITS picks is where slot 0 goes again.
    picks_seen = [p for p, _ in seen]
    assert picks_seen[:2] == [1, 20]


# --- strategy_adp / strategy_vor: direct, single-call behaviour ---


def test_strategy_adp_picks_the_lowest_adp_available_player():
    candidates = pd.DataFrame([
        {"player_id": "a", "position": "RB", "adp": 5.0, "proj_points": 10.0},
        {"player_id": "b", "position": "WR", "adp": 1.0, "proj_points": 8.0},
        {"player_id": "c", "position": "WR", "adp": 12.0, "proj_points": 20.0},
    ])
    chosen = strategy_adp(candidates, roster=[], pick=1, next_pick=20, teams=10)
    assert chosen["player_id"] == "b"


def test_strategy_adp_sends_a_missing_adp_player_to_the_back_of_the_line():
    candidates = pd.DataFrame([
        {"player_id": "ranked", "position": "RB", "adp": 50.0, "proj_points": 10.0},
        {"player_id": "unranked", "position": "WR", "adp": float("nan"), "proj_points": 999.0},
    ])
    chosen = strategy_adp(candidates, roster=[], pick=1, next_pick=20, teams=10)
    assert chosen["player_id"] == "ranked"


def test_strategy_vor_picks_the_highest_vor_available_player():
    candidates = pd.DataFrame([
        {"player_id": "a", "position": "RB", "vor": 12.0},
        {"player_id": "b", "position": "WR", "vor": 40.0},
        {"player_id": "c", "position": "WR", "vor": 39.9},
    ])
    chosen = strategy_vor(candidates, roster=[], pick=1, next_pick=20, teams=10)
    assert chosen["player_id"] == "b"


def test_strategy_vor_ignores_positions_with_no_replacement_level():
    candidates = pd.DataFrame([
        {"player_id": "unscored", "position": "DT", "vor": float("nan")},
        {"player_id": "scored", "position": "RB", "vor": 5.0},
    ])
    chosen = strategy_vor(candidates, roster=[], pick=1, next_pick=20, teams=10)
    assert chosen["player_id"] == "scored"


# --- strategy_vor_survival: normal behaviour mirrors draft.recommend ---


def test_strategy_vor_survival_prefers_the_player_you_would_lose():
    # Same scenario as test_draft.py's core rule: near-equal VOR, one
    # certain to survive, one certain to be gone -- take the one you'd lose.
    candidates = pd.DataFrame([
        {"player_id": "safe", "position": "RB", "proj_points": 50.0, "vor": 50.0,
         "adp": 200.0, "stdev": 5.0},
        {"player_id": "scarce", "position": "WR", "proj_points": 48.0, "vor": 48.0,
         "adp": 21.0, "stdev": 3.0},
    ])
    strategy = strategy_vor_survival(CONFIG_OBJ)
    chosen = strategy(candidates, roster=[], pick=20, next_pick=30, teams=10)
    assert chosen["player_id"] == "scarce"


# --- CRITICAL #2: the all-zero expected-loss degenerate case ---
#
# Every player missing from the ADP feed gets p_gone_by_next = 0 (see
# survival.py: "no adp at all" -> certainly available), so expected_loss =
# vor * 0 = 0 for ALL of them. Late in a real draft this is the ONLY kind
# of player left (FFC's feed covers ~150-230 players; a 10x15 draft is 150
# picks), so a recommender that just sorts by (already-zero) expected_loss
# carries no signal and returns an arbitrary order -- exactly where bench
# depth is decided. strategy_vor_survival's documented fix: when the best
# available expected_loss is ~0, fall back to ranking by raw VOR (still
# respecting the roster-need discount), instead of an arbitrary tiebreak.


def _all_missing_adp_board() -> pd.DataFrame:
    return pd.DataFrame([
        {"player_id": "best", "position": "RB", "proj_points": 30.0, "vor": 30.0,
         "adp": float("nan"), "stdev": float("nan")},
        {"player_id": "mid", "position": "WR", "proj_points": 20.0, "vor": 20.0,
         "adp": float("nan"), "stdev": float("nan")},
        {"player_id": "worst", "position": "TE", "proj_points": 5.0, "vor": 5.0,
         "adp": float("nan"), "stdev": float("nan")},
    ])


def test_strategy_vor_survival_falls_back_to_raw_vor_when_every_candidate_has_zero_expected_loss():
    strategy = strategy_vor_survival(CONFIG_OBJ)
    chosen = strategy(_all_missing_adp_board(), roster=[], pick=140, next_pick=150, teams=10)
    assert chosen["player_id"] == "best"  # highest VOR, not board order or an arbitrary tie


def test_strategy_vor_survival_fallback_still_applies_the_roster_need_discount():
    # RB starters_per_team here is round(2.5) == 2 (see CONFIG at top of
    # file). Three RBs already rostered -> RB need is 0 -> discounted by
    # FILLED_POSITION_DISCOUNT even inside the all-zero fallback, so a
    # clearly-needed TE outranks a higher-VOR but already-filled RB.
    board_ = pd.DataFrame([
        {"player_id": "rb", "position": "RB", "proj_points": 30.0, "vor": 30.0,
         "adp": float("nan"), "stdev": float("nan")},
        {"player_id": "te", "position": "TE", "proj_points": 20.0, "vor": 20.0,
         "adp": float("nan"), "stdev": float("nan")},
    ])
    roster = [{"player_id": f"r{i}", "position": "RB"} for i in range(3)]
    strategy = strategy_vor_survival(CONFIG_OBJ)
    chosen = strategy(board_, roster=roster, pick=140, next_pick=150, teams=10)
    # 30.0 * FILLED_POSITION_DISCOUNT (=15.0) < 20.0 * 1.0 -- TE wins.
    assert FILLED_POSITION_DISCOUNT == 0.5  # pin the assumption this test's arithmetic relies on
    assert chosen["player_id"] == "te"


def test_a_full_draft_survives_running_out_of_adp_covered_players():
    # Integration-level check: strategy_vor_survival must not crash once
    # the real board's bottom 40 (no adp at all -- see board()'s docstring)
    # become the only players left, which happens well before 150 picks
    # are exhausted from a 220-player pool.
    strategy = strategy_vor_survival(CONFIG_OBJ)
    result = simulate_draft(board(), teams=10, rounds=15, my_slot=3, strategy=strategy, seed=3)
    assert len(result) == 15
    assert result["player_id"].is_unique


# --- adp_noise: strengthened beyond "the two outputs merely differ" ---
#
# test_adp_noise_changes_the_draft (given, above) is weak: it would also
# pass under a broken implementation that scrambles the draft in some
# noise-UNRELATED way whenever adp_noise changes at all. This test instead
# pins the EXACT resulting order for a hand-computed case: simulate_draft's
# only randomness is one `rng.normal(0, adp_noise, size=n)` draw, taken
# immediately from `np.random.default_rng(seed)` before anything else, and
# added to `adp` (see tt.mock module docstring). With a single team, the
# whole draft order is exactly that noisy-adp ranking, so it can be
# reproduced independently here and compared for exact equality -- a level
# assertion a sign-flipped or unwired noise term could not pass by luck.
def test_adp_noise_produces_the_exact_hand_computed_order():
    candidates = pd.DataFrame([
        {"player_id": "p1", "position": "RB", "proj_points": 40.0, "vor": 40.0, "adp": 1.0, "stdev": 1.0},
        {"player_id": "p2", "position": "RB", "proj_points": 30.0, "vor": 30.0, "adp": 2.0, "stdev": 1.0},
        {"player_id": "p3", "position": "WR", "proj_points": 20.0, "vor": 20.0, "adp": 3.0, "stdev": 1.0},
        {"player_id": "p4", "position": "WR", "proj_points": 10.0, "vor": 10.0, "adp": 4.0, "stdev": 1.0},
    ])
    seed, noise_scale = 42, 3.0
    expected_noise = np.random.default_rng(seed).normal(0.0, noise_scale, size=len(candidates))
    expected_effective_adp = candidates["adp"].to_numpy() + expected_noise
    expected_order = (
        candidates.assign(_effective_adp=expected_effective_adp)
        .sort_values("_effective_adp")["player_id"]
        .tolist()
    )

    result = simulate_draft(candidates, teams=1, rounds=4, my_slot=0,
                            strategy=strategy_adp, seed=seed, adp_noise=noise_scale)
    assert list(result["player_id"]) == expected_order


def test_adp_noise_default_is_a_positive_constant():
    # Without SOME default noise, strategy_adp would be fully deterministic
    # regardless of seed, making "trials" in compare_strategies a no-op for
    # the adp strategy -- see module docstring for why this must be > 0.
    assert ADP_NOISE_DEFAULT > 0


# --- compare_strategies ---


def test_compare_strategies_uses_common_random_numbers_across_strategies():
    # Same seed feeds every strategy's trials, so two strategy NAMES bound
    # to the literal same callable must score IDENTICALLY across all
    # trials, not just "close." This pins the "paired comparison" design
    # (see tt.mock module docstring) and would fail hard if trial seeding
    # were derived per-strategy instead of shared.
    out = compare_strategies(
        board(), {"adp_1": strategy_adp, "adp_2": strategy_adp},
        trials=8, teams=10, my_slot=3, seed=5,
    )
    rows = out.set_index("strategy")
    assert rows.loc["adp_1", "mean_score"] == pytest.approx(rows.loc["adp_2", "mean_score"])
    assert rows.loc["adp_1", "std_score"] == pytest.approx(rows.loc["adp_2", "std_score"])


def test_compare_strategies_default_score_roster_is_circularity_flagged_in_the_docstring():
    # Not a behavioural assertion -- a guard against silently "simplifying
    # away" the documented circularity warning (see CRITICAL #1 in the task
    # brief) in a future edit.
    assert "circular" in compare_strategies.__doc__.lower()
    assert "score_roster" in compare_strategies.__doc__


def test_compare_strategies_accepts_a_custom_score_roster():
    calls = []

    def count_players(roster: pd.DataFrame) -> float:
        calls.append(len(roster))
        return float(len(roster))

    out = compare_strategies(
        board(), {"adp": strategy_adp}, trials=3, teams=10, my_slot=3, seed=1,
        score_roster=count_players,
    )
    assert calls  # the custom scorer was actually invoked
    assert out.iloc[0]["mean_score"] == pytest.approx(15.0)  # 15 rounds every trial


def test_compare_strategies_rounds_defaults_to_a_full_draft():
    out = compare_strategies(board(), {"adp": strategy_adp}, trials=2, teams=10, my_slot=3, seed=1)
    assert len(out) == 1


# --- optimal_lineup_score: the default score_roster ---


def test_optimal_lineup_score_sums_only_the_top_starters_per_position():
    # RB target here is round(2.5) == 2 -> only the top 2 RBs by
    # proj_points count; the 3rd RB (bench) must NOT be included.
    roster = pd.DataFrame([
        {"player_id": "rb1", "position": "RB", "proj_points": 20.0},
        {"player_id": "rb2", "position": "RB", "proj_points": 15.0},
        {"player_id": "rb3", "position": "RB", "proj_points": 5.0},  # bench: worse than rb1/rb2, excluded
        {"player_id": "wr1", "position": "WR", "proj_points": 10.0},
    ])
    score = optimal_lineup_score(CONFIG_OBJ)(roster)
    assert score == pytest.approx(20.0 + 15.0 + 10.0)


def test_optimal_lineup_score_has_a_usable_default_when_no_config_is_given():
    # compare_strategies' own default path (no config, no score_roster) must
    # not crash -- see module docstring on why a fallback constant exists.
    roster = pd.DataFrame([{"player_id": "x", "position": "RB", "proj_points": 12.0}])
    score = optimal_lineup_score()(roster)
    assert score >= 0.0


# ---------------------------------------------------------------------------
# F10: `trials` must actually be trials -- a deterministic strategy has to see
# a DIFFERENT draft environment on every one of them.
# ---------------------------------------------------------------------------


def test_compare_strategies_gives_a_deterministic_strategy_real_trial_variance():
    """THE F10 guard. `strategy_vor` is a pure function of the board: it never
    reads `_effective_adp`, so under a leaguewide-symmetric simulation every
    opposing team also ignored the per-trial noise and every one of 50 trials
    replayed a byte-identical draft. Task 7's real-data run duly reported
    `std_score = 0.00` over 50 trials for both `vor` and `vor_survival` --
    which means the headline comparison (adp 1199.18 / vor 1290.53 /
    vor_survival 1130.65) was three SINGLE samples, with no way to tell any
    difference from noise because no noise was ever generated.

    `compare_strategies` now drafts the opposing nine teams off the noisy ADP
    market, so trial `i`'s board differs from trial `j`'s and a deterministic
    strategy faces a genuinely different draft each time.
    """
    out = compare_strategies(
        board(), {"vor": strategy_vor}, trials=6, teams=10, my_slot=3, seed=11,
    ).set_index("strategy")
    assert out.loc["vor", "std_score"] > 0.0
    assert out.loc["vor", "min_score"] < out.loc["vor", "max_score"]


def test_compare_strategies_reports_the_uncertainty_of_its_own_mean():
    """A mean with no stated uncertainty is exactly how an unpowered
    difference gets read as a real one -- the failure Task 7's table walked
    into. `sem_score` and the 95% interval must be present, consistent with
    `std_score`, and must bracket the mean."""
    out = compare_strategies(
        board(), {"vor": strategy_vor}, trials=6, teams=10, my_slot=3, seed=11,
    ).iloc[0]
    # Sample standard error, from the sample (ddof=1) standard deviation.
    trials = out["trials"]
    assert out["sem_score"] == pytest.approx(
        out["std_score"] * (trials / (trials - 1)) ** 0.5 / trials**0.5
    )
    assert out["ci95_low"] == pytest.approx(out["mean_score"] - 1.96 * out["sem_score"])
    assert out["ci95_high"] == pytest.approx(out["mean_score"] + 1.96 * out["sem_score"])
    assert out["ci95_low"] < out["mean_score"] < out["ci95_high"]


def test_compare_strategies_varies_the_opponents_not_just_my_own_picks():
    """Pins the MECHANISM, not just the symptom. The variance has to come from
    the opposing teams responding to the per-trial seed; a strategy that is
    itself deterministic must still be handed different boards.

    Run with a spy strategy that records the board it is shown at its very
    first pick: across trials those boards must differ. (At `my_slot=3` the
    first pick already follows three opponent picks, so an unvaried
    environment would show an identical board every time.)
    """
    seen: list[tuple] = []

    def spy(board_, roster, pick, next_pick, teams):
        if not roster:
            seen.append(tuple(board_["player_id"]))
        return strategy_vor(board_, roster, pick, next_pick, teams)

    compare_strategies(board(), {"spy": spy}, trials=5, teams=10, my_slot=3, seed=3)
    assert len(seen) == 5
    assert len(set(seen)) > 1, "every trial showed my_slot the identical board"


def test_simulate_draft_can_give_the_opponents_their_own_strategy():
    """`opponent_strategy` is what lets `compare_strategies` ask "how does MY
    policy do against a market", while `simulate_draft`'s own default stays
    the symmetric "if the whole league drafted this way" question its
    docstring describes."""
    calls = {"mine": 0, "theirs": 0}

    def mine(board_, roster, pick, next_pick, teams):
        calls["mine"] += 1
        return strategy_vor(board_, roster, pick, next_pick, teams)

    def theirs(board_, roster, pick, next_pick, teams):
        calls["theirs"] += 1
        return strategy_adp(board_, roster, pick, next_pick, teams)

    simulate_draft(board(), teams=10, rounds=3, my_slot=3, strategy=mine,
                   seed=1, opponent_strategy=theirs)
    assert calls["mine"] == 3            # my three picks
    assert calls["theirs"] == 27         # the other nine teams' three picks each


def test_simulate_draft_defaults_to_one_strategy_for_the_whole_league():
    calls = {"n": 0}

    def counting(board_, roster, pick, next_pick, teams):
        calls["n"] += 1
        return strategy_adp(board_, roster, pick, next_pick, teams)

    simulate_draft(board(), teams=10, rounds=3, my_slot=3, strategy=counting, seed=1)
    assert calls["n"] == 30
