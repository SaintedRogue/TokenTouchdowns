"""The lineup optimiser: `argmax` expected points over LEGAL lineups, FLEX
resolved last (docs/draft-engine-design.md 3.6).

This module extracts the slot-filling algorithm `tt.mock.optimal_lineup_score`
already got right (fixed roster slots filled first, top-`count` by points at
that exact position; flex slots filled ONLY from what's left over) so there
is exactly one implementation of "how do you legally fill a fantasy lineup"
in this codebase, and gives it a shape that can also answer "who, and in
which slot" -- not just the point total `optimal_lineup_score` returns.

THE FLEX-LAST TEST BELOW IS THE POINT OF THIS FILE. Filling the flex slot
greedily -- "give it to the single best flex-eligible player on the roster,
before anything else is assigned" -- can strand a FIXED slot that has no
legal filler left, because a flex-eligible player (e.g. an RB) can also be
the only thing standing between a fixed RB slot and going unfilled. Fixed
slots first, flex from the remainder, is what guarantees every fixed slot
gets first claim on the players ONLY it can use.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tt.league import load_config_from_dict
from tt.lineup import lineup_points, optimal_lineup

# A minimal, hand-computable 2-way flex league: 2 fixed RB slots plus one
# RB/WR flex slot ("W/R" in tt.league.FLEX_ELIGIBLE). No QB/WR-fixed/TE/K/DEF
# slots at all -- keeping every other position's target at 0 isolates the
# RB/flex interaction the stranding test is about, so a failure can only be
# about THAT interaction, not some other slot's bookkeeping.
_RB_FLEX_CONFIG = load_config_from_dict({
    "leagueKey": "x", "name": "x", "numTeams": 10, "maxTeams": 10,
    "draftStatus": "predraft",
    "rosterSlots": {"RB": 2, "W/R": 1},
    "scoring": [],
})

# The real league's shape, reused from test_mock.py's `_REAL_SHAPE_CONFIG`:
# 1 QB, 2 RB, 2 WR, 1 TE, 1 W/R/T flex, 1 K, 1 DEF -- 9 starters total.
_REAL_SHAPE_CONFIG = load_config_from_dict({
    "leagueKey": "x", "name": "x", "numTeams": 10, "maxTeams": 10,
    "draftStatus": "predraft",
    "rosterSlots": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1},
    "scoring": [],
})


# ---------------------------------------------------------------------------
# FLEX RESOLVED LAST -- the point of this module.
# ---------------------------------------------------------------------------

def test_flex_first_would_strand_a_fixed_rb_slot_but_optimal_lineup_does_not():
    # Only 2 RBs exist on this roster. A naive "fill the flex slot first,
    # from the single best flex-eligible player leaguewide" algorithm would
    # award the flex to RB_A (30, the single best flex-eligible player of
    # all four), leaving only RB_B for the two REQUIRED fixed RB slots --
    # mathematically impossible to fill both from one remaining RB. Prove
    # that stranding is real, then prove `optimal_lineup` avoids it.
    roster = pd.DataFrame([
        {"player_id": "rb_a", "position": "RB", "proj_points": 30.0},
        {"player_id": "rb_b", "position": "RB", "proj_points": 25.0},
        {"player_id": "wr_a", "position": "WR", "proj_points": 10.0},
        {"player_id": "wr_b", "position": "WR", "proj_points": 8.0},
    ])

    # --- demonstrate the naive flex-first failure mode ---
    flex_eligible = roster[roster["position"].isin(("RB", "WR"))].sort_values(
        "proj_points", ascending=False
    )
    naive_flex_pick = flex_eligible.iloc[0]
    assert naive_flex_pick["player_id"] == "rb_a"  # the single best flex-eligible player
    remaining_rbs = roster[(roster["position"] == "RB") & (roster["player_id"] != naive_flex_pick["player_id"])]
    # Only 1 RB left after the naive flex pick, but the fixed RB slot needs 2:
    # a flex-first algorithm cannot legally fill this roster.
    assert len(remaining_rbs) == 1

    # --- the real implementation must not do this ---
    lineup = optimal_lineup(roster, _RB_FLEX_CONFIG)
    starters = lineup[lineup["starter"]]
    rb_starters = set(starters[starters["slot"] == "RB"]["player_id"])
    assert rb_starters == {"rb_a", "rb_b"}  # both RBs fill the two fixed slots
    flex_starters = starters[starters["slot"] == "W/R"]["player_id"].tolist()
    assert flex_starters == ["wr_a"]  # flex goes to the best REMAINING flex-eligible player
    assert lineup_points(roster, _RB_FLEX_CONFIG) == pytest.approx(30.0 + 25.0 + 10.0)


def test_optimal_lineup_awards_flex_to_best_remaining_on_real_league_shape():
    # Cross-check against test_mock.py's own flex fixture, but asserting the
    # actual SLOT ASSIGNMENT (not just the point total, which -- with all 9
    # players exactly filling 9 slots -- would be the same regardless of
    # who goes where). `wr3` (5.0) is the single best WR of the three, so
    # the FIXED WR slot (top-2 at the WR position) claims it outright,
    # displacing one of the tied 1.0-point WRs into the flex slot instead
    # -- flex does NOT simply grab the highest-value leftover WR by design,
    # it only ever gets what the fixed slot didn't already take.
    roster = pd.DataFrame([
        {"player_id": "qb1", "position": "QB", "proj_points": 1.0},
        {"player_id": "rb1", "position": "RB", "proj_points": 1.0},
        {"player_id": "rb2", "position": "RB", "proj_points": 1.0},
        {"player_id": "wr1", "position": "WR", "proj_points": 1.0},
        {"player_id": "wr2", "position": "WR", "proj_points": 1.0},
        {"player_id": "te1", "position": "TE", "proj_points": 1.0},
        {"player_id": "k1", "position": "K", "proj_points": 1.0},
        {"player_id": "def1", "position": "DEF", "proj_points": 1.0},
        {"player_id": "wr3", "position": "WR", "proj_points": 5.0},
    ])
    lineup = optimal_lineup(roster, _REAL_SHAPE_CONFIG)
    starters = lineup[lineup["starter"]]
    assert set(starters["player_id"]) == {
        "qb1", "rb1", "rb2", "wr1", "wr2", "te1", "k1", "def1", "wr3",
    }
    wr_fixed = set(starters[starters["slot"] == "WR"]["player_id"])
    assert wr_fixed == {"wr3", "wr1"}  # top-2 WR by points; wr1 beats tied wr2 (input order)
    flex_row = starters[starters["slot"] == "W/R/T"]
    assert len(flex_row) == 1
    assert flex_row.iloc[0]["player_id"] == "wr2"  # what's left after the fixed WR slot
    bench = lineup[~lineup["starter"]]
    assert bench.empty  # exactly 9 players for exactly 9 starting slots


# ---------------------------------------------------------------------------
# NaN points must never silently win a slot.
# ---------------------------------------------------------------------------

def test_nan_points_player_does_not_win_a_slot_even_when_it_would_fit_by_count():
    # 2 RB fixed slots, exactly 2 RB-position rows -- but one has NaN
    # points. A naive `sort_values(...).head(2)` grabs BOTH rows whenever
    # there are only 2 to begin with, regardless of sort order, so without
    # an explicit NaN guard the NaN player would silently start. It must
    # instead be excluded, leaving the second RB slot legitimately EMPTY.
    roster = pd.DataFrame([
        {"player_id": "rb1", "position": "RB", "proj_points": 20.0},
        {"player_id": "rb_nan", "position": "RB", "proj_points": np.nan},
    ])
    config = load_config_from_dict({
        "leagueKey": "x", "name": "x", "numTeams": 10, "maxTeams": 10,
        "draftStatus": "predraft", "rosterSlots": {"RB": 2}, "scoring": [],
    })
    lineup = optimal_lineup(roster, config)
    rb_rows = lineup[lineup["slot"] == "RB"]
    assert len(rb_rows) == 2  # both RB slots are named, even though one is empty
    filled = rb_rows[rb_rows["player_id"] == "rb1"]
    assert len(filled) == 1
    assert bool(filled.iloc[0]["starter"]) is True
    empty = rb_rows[rb_rows["player_id"].isna()]
    assert len(empty) == 1
    assert bool(empty.iloc[0]["starter"]) is True
    assert bool(empty.iloc[0]["empty"]) is True
    # The NaN player must appear on the bench, not as a starter anywhere.
    nan_row = lineup[lineup["player_id"] == "rb_nan"]
    assert len(nan_row) == 1
    assert bool(nan_row.iloc[0]["starter"]) is False
    assert lineup_points(roster, config) == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# Thin rosters: a missing position must produce a NAMED empty slot, never a
# silently short lineup (the exact defect class fixed in mock.py's M-4).
# ---------------------------------------------------------------------------

def test_thin_roster_names_the_empty_slot_instead_of_returning_fewer_starters():
    # No QB on the roster at all. QB is a fixed, non-flex slot, so nothing
    # can rescue it -- the correct behaviour is a starter ROW for QB with no
    # player, not a lineup that is just quietly one starter short.
    roster = pd.DataFrame([
        {"player_id": "rb1", "position": "RB", "proj_points": 12.0},
        {"player_id": "rb2", "position": "RB", "proj_points": 10.0},
        {"player_id": "wr1", "position": "WR", "proj_points": 9.0},
        {"player_id": "wr2", "position": "WR", "proj_points": 7.0},
        {"player_id": "te1", "position": "TE", "proj_points": 6.0},
        {"player_id": "k1", "position": "K", "proj_points": 5.0},
        {"player_id": "def1", "position": "DEF", "proj_points": 4.0},
    ])
    lineup = optimal_lineup(roster, _REAL_SHAPE_CONFIG)
    total_starting_slots = sum(_REAL_SHAPE_CONFIG.roster_slots.values())
    starter_rows = lineup[lineup["starter"]]
    assert len(starter_rows) == total_starting_slots  # 9 rows, never fewer
    qb_rows = starter_rows[starter_rows["slot"] == "QB"]
    assert len(qb_rows) == 1
    assert pd.isna(qb_rows.iloc[0]["player_id"])
    assert bool(qb_rows.iloc[0]["empty"]) is True
    # The other 8 slots are filled from the 7 remaining players plus the
    # flex slot has nothing left to award (also thin) -- flex must ALSO be
    # named empty, not silently dropped.
    flex_rows = starter_rows[starter_rows["slot"] == "W/R/T"]
    assert len(flex_rows) == 1
    assert bool(flex_rows.iloc[0]["empty"]) is True
    assert lineup_points(roster, _REAL_SHAPE_CONFIG) == pytest.approx(
        12.0 + 10.0 + 9.0 + 7.0 + 6.0 + 5.0 + 4.0
    )


# ---------------------------------------------------------------------------
# Determinism: ties must resolve the same way every run, tracked to input
# order (a stable tiebreak), not to whatever an unstable sort settles on.
# ---------------------------------------------------------------------------

def test_tied_points_resolve_deterministically_by_input_order():
    config = load_config_from_dict({
        "leagueKey": "x", "name": "x", "numTeams": 10, "maxTeams": 10,
        "draftStatus": "predraft", "rosterSlots": {"WR": 1}, "scoring": [],
    })
    roster_first = pd.DataFrame([
        {"player_id": "wr_first", "position": "WR", "proj_points": 10.0},
        {"player_id": "wr_second", "position": "WR", "proj_points": 10.0},
    ])
    roster_reversed = roster_first.iloc[::-1].reset_index(drop=True)

    lineup_a = optimal_lineup(roster_first, config)
    lineup_b = optimal_lineup(roster_first, config)
    winner_a = lineup_a[lineup_a["starter"]].iloc[0]["player_id"]
    winner_b = lineup_b[lineup_b["starter"]].iloc[0]["player_id"]
    assert winner_a == winner_b == "wr_first"  # repeat runs agree, and pick input-order-first

    lineup_reversed = optimal_lineup(roster_reversed, config)
    winner_reversed = lineup_reversed[lineup_reversed["starter"]].iloc[0]["player_id"]
    assert winner_reversed == "wr_second"  # tiebreak tracks INPUT order, not player identity


# ---------------------------------------------------------------------------
# points_column is a real parameter, not hardcoded to proj_points.
# ---------------------------------------------------------------------------

def test_points_column_parameter_is_honoured():
    config = load_config_from_dict({
        "leagueKey": "x", "name": "x", "numTeams": 10, "maxTeams": 10,
        "draftStatus": "predraft", "rosterSlots": {"WR": 1}, "scoring": [],
    })
    roster = pd.DataFrame([
        {"player_id": "wr_high_proj_low_p10", "position": "WR", "proj_points": 20.0, "p10": 2.0},
        {"player_id": "wr_low_proj_high_p10", "position": "WR", "proj_points": 5.0, "p10": 15.0},
    ])
    default_lineup = optimal_lineup(roster, config)
    default_winner = default_lineup[default_lineup["starter"]].iloc[0]["player_id"]
    assert default_winner == "wr_high_proj_low_p10"

    p10_lineup = optimal_lineup(roster, config, points_column="p10")
    p10_winner = p10_lineup[p10_lineup["starter"]].iloc[0]["player_id"]
    assert p10_winner == "wr_low_proj_high_p10"
    assert lineup_points(roster, config, points_column="p10") == pytest.approx(15.0)
