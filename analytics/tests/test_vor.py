"""Value over replacement and tiering. See `tt.vor` module docstring for why
raw proj_points ranks QBs at the top of every board and VOR is the fix.

Test config deliberately uses a "W/R" (RB/WR only) flex slot, not the real
league's "W/R/T", so RB and WR starters_per_team land on a clean 2.5 (2 +
1/2 flex) instead of the real league's 2.333... (2 + 1/3 flex) -- the exact
`round(2.5 * teams)` arithmetic in the brief's own tests only holds for a
2-way flex split.
"""
import pandas as pd
import pytest

from tt.league import load_config_from_dict
from tt.vor import add_vor, replacement_levels

CONFIG = {
    "leagueKey": "470.l.1433971", "name": "Test League",
    "numTeams": 4, "maxTeams": 10, "draftStatus": "predraft",
    "rosterSlots": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "W/R": 1, "K": 1, "DEF": 1},
    "scoring": [
        {"statId": 4, "name": "Pass Yds", "group": "passing", "value": 0.04},
        {"statId": 5, "name": "Pass TD", "group": "passing", "value": 4},
        {"statId": 6, "name": "Int", "group": "passing", "value": -1},
        {"statId": 9, "name": "Rush Yds", "group": "rushing", "value": 0.1},
        {"statId": 10, "name": "Rush TD", "group": "rushing", "value": 6},
        {"statId": 11, "name": "Rec", "group": "receiving", "value": 0.5},
        {"statId": 12, "name": "Rec Yds", "group": "receiving", "value": 0.1},
        {"statId": 13, "name": "Rec TD", "group": "receiving", "value": 6},
        {"statId": 18, "name": "Fum Lost", "group": "fumbles", "value": -2},
    ],
}
CONFIG_OBJ = load_config_from_dict(CONFIG)


def fake_projection_table() -> pd.DataFrame:
    """40 RBs, descending points, no ties -- so "the player at rank N" is
    unambiguous regardless of sort implementation, and every VOR value below
    the replacement rank is strictly negative (never merely zero)."""
    return pd.DataFrame({
        "player_id": [f"RB{i}" for i in range(40)],
        "position": "RB",
        "proj_points": [300.0 - 3.0 * i for i in range(40)],
    })


def table_with_an_obvious_cliff() -> pd.DataFrame:
    """One position, 10 players. Gaps between adjacent players are a flat 2.0
    points everywhere except between rank 3 and rank 4, where it jumps to
    36 points -- an unmissable cliff amid otherwise uniform spacing."""
    points = [100.0, 98.0, 96.0, 60.0, 58.0, 56.0, 54.0, 52.0, 50.0, 48.0]
    return pd.DataFrame({
        "player_id": [f"WR{i}" for i in range(len(points))],
        "position": "WR",
        "proj_points": points,
    })


def test_replacement_level_scales_with_team_count():
    # THE most consequential number in the draft engine. At 10 teams 20 RBs
    # start; at 4 teams only 8 do, so RB10-20 go from starters to waiver fodder.
    deep = replacement_levels(CONFIG_OBJ, teams=10)
    shallow = replacement_levels(CONFIG_OBJ, teams=4)
    assert deep["RB"] > shallow["RB"]
    assert deep["RB"] == pytest.approx(round(2.5 * 10))
    assert shallow["RB"] == pytest.approx(round(2.5 * 4))


def test_replacement_level_has_no_default_team_count():
    # The real league has 4 teams joined of a 10-team max; defaulting to
    # either would silently produce a board sized for the wrong league.
    with pytest.raises(TypeError):
        replacement_levels(CONFIG_OBJ)


def test_vor_is_zero_at_the_replacement_player():
    proj = fake_projection_table()  # 40 RBs, descending points
    out = add_vor(proj, CONFIG_OBJ, teams=4)
    level = replacement_levels(CONFIG_OBJ, teams=4)["RB"]
    at_replacement = out[out.position == "RB"].sort_values("proj_points", ascending=False)
    assert at_replacement.iloc[level - 1]["vor"] == pytest.approx(0.0, abs=1e-9)


def test_vor_is_negative_below_replacement():
    out = add_vor(fake_projection_table(), CONFIG_OBJ, teams=4)
    rbs = out[out.position == "RB"].sort_values("proj_points", ascending=False)
    assert rbs.iloc[-1]["vor"] < 0


def test_shallower_leagues_compress_vor_at_the_position():
    # With fewer teams, replacement is a better player, so everyone's VOR falls.
    proj = fake_projection_table()
    deep = add_vor(proj, CONFIG_OBJ, teams=10).set_index("player_id")["vor"]
    shallow = add_vor(proj, CONFIG_OBJ, teams=4).set_index("player_id")["vor"]
    assert shallow.max() < deep.max()


def test_tiers_break_at_the_largest_value_gaps():
    out = add_vor(table_with_an_obvious_cliff(), CONFIG_OBJ, teams=10)
    top = out.sort_values("vor", ascending=False)
    assert top.iloc[0]["tier"] == 1
    # The cliff player starts a new tier.
    assert top.iloc[3]["tier"] > top.iloc[2]["tier"]


def test_add_vor_gives_nan_for_positions_with_no_replacement_level():
    # nflverse tags ~25 positions (LB, DT, OT, ...); this league only starts
    # QB/RB/WR/TE. A defensive lineman sneaking into a projections table
    # (e.g. a bad upstream filter) must not silently get a VOR/tier that
    # implies this league drafts the position.
    proj = pd.DataFrame({
        "player_id": ["DT1"], "position": ["DT"], "proj_points": [40.0],
    })
    out = add_vor(proj, CONFIG_OBJ, teams=10)
    assert out.iloc[0]["vor"] != out.iloc[0]["vor"]  # NaN != NaN
    assert out.iloc[0]["tier"] != out.iloc[0]["tier"]


def test_add_vor_handles_replacement_rank_beyond_the_available_player_pool():
    # A deep league (teams=10 -> RB replacement rank 25) queried against a
    # thin dataset (5 RBs) has no literal 25th player. Falling back to the
    # worst available player must not raise, and VOR at that worst player
    # must land at exactly 0 (it *is* the fallback replacement).
    thin = pd.DataFrame({
        "player_id": [f"RB{i}" for i in range(5)],
        "position": "RB",
        "proj_points": [50.0, 40.0, 30.0, 20.0, 10.0],
    })
    out = add_vor(thin, CONFIG_OBJ, teams=10)
    worst = out.sort_values("proj_points", ascending=False).iloc[-1]
    assert worst["vor"] == pytest.approx(0.0, abs=1e-9)


def realistically_spaced_table() -> pd.DataFrame:
    """60 WRs with the HETEROSCEDASTIC spacing a real position actually has.

    The old fixture (`table_with_an_obvious_cliff`) has uniform 2.0-point
    gaps everywhere but one, and a uniform gap distribution structurally
    cannot exhibit the bug that broke tiering on real data: a real position
    is 200-340 near-identical replacement-level players (measured gaps of
    0.26-0.42) under a top of the board spaced 4-19 points apart, so a median
    taken over the WHOLE position is set by the tail and every adjacent pair
    at the top clears 2x it. Measured consequence on the real 2023-25 board:
    65 tiers for 227 RBs, with the top 12 RBs in tiers 1..12 -- one each.

    This fixture reproduces that shape at 1/5 scale, against a replacement
    level of 25 (teams=10, RB/WR = 2.5 starters each):

      ranks  0-2   a genuine top cluster, 1.0-point gaps
      rank   3     a 40-point cliff
      ranks  3-5   a second cluster, 1.0-point gaps
      rank   6     a 4.0-point break -- REAL but modest; it clears 2x the
                   median gap (2.0) and does NOT clear 2x the mean (5.5),
                   which is what makes the median-vs-mean choice testable
      ranks  6-24  routine 1.0-point spacing
      ranks 25-59  the flat replacement tail, 0.3-point gaps

    Expected: exactly three tiers, of sizes 3, 3 and 54.
    """
    points = [300.0, 299.0, 298.0]           # tier 1
    points += [258.0, 257.0, 256.0]          # 40.0 cliff -> tier 2
    points += [252.0]                        # 4.0 break  -> tier 3
    for _ in range(18):                      # routine 1.0 spacing to rank 24
        points.append(points[-1] - 1.0)
    for _ in range(35):                      # flat replacement tail
        points.append(points[-1] - 0.3)
    assert len(points) == 60
    return pd.DataFrame({
        "player_id": [f"WR{i:03d}" for i in range(len(points))],
        "position": "WR",
        "proj_points": points,
    })


def test_tiers_cluster_the_top_of_the_board_instead_of_ranking_it():
    """THE F4 guard, asserting LEVELS (how many tiers, and who shares one),
    not a direction. A direction (`iloc[3].tier > iloc[2].tier`) is satisfied
    by every degenerate variant -- including one that gives all 60 players
    their own tier -- which is why the old test passed a board where `tier`
    was a rank."""
    out = add_vor(realistically_spaced_table(), CONFIG_OBJ, teams=10)
    ranked = out.sort_values("vor", ascending=False).reset_index(drop=True)

    assert ranked["tier"].nunique() == 3
    assert list(ranked["tier"].head(7)) == [1.0, 1.0, 1.0, 2.0, 2.0, 2.0, 3.0]
    assert (ranked["tier"].iloc[6:] == 3.0).all()
    # The point of the column: the top of the board must say "these are
    # interchangeable", not "these are ranked 1 through 12".
    assert ranked["tier"].head(12).nunique() < 12


def test_the_tier_threshold_ignores_the_flat_replacement_tail():
    """Pins the MECHANISM. The reference gap is taken over the players at or
    above replacement level; the sub-replacement tail must not be able to
    drag it down. Deepening that tail with more near-identical players (which
    is what a real position's stat sheet does) must not change a single tier
    at the top."""
    base = realistically_spaced_table()
    deeper = base.copy()
    extra = [{"player_id": f"WRX{i:03d}", "position": "WR",
              "proj_points": base["proj_points"].min() - 0.3 * (i + 1)}
             for i in range(120)]
    deeper = pd.concat([deeper, pd.DataFrame(extra)], ignore_index=True)

    tiers_base = add_vor(base, CONFIG_OBJ, teams=10).set_index("player_id")["tier"]
    tiers_deep = add_vor(deeper, CONFIG_OBJ, teams=10).set_index("player_id")["tier"]
    for player_id in base["player_id"]:
        assert tiers_base[player_id] == tiers_deep[player_id]


def tie_heavy_table() -> pd.DataFrame:
    """A position whose players are mostly TIED, with one real cliff.

    Gaps: seven exact zeros, then 1.0, 1.0, a 38.0 cliff, 1.0. The median of
    the NONZERO gaps is 1.0 (threshold 2.0, so the cliff breaks and nothing
    else does); the median including the zeros is 0.0, which the code turns
    into an infinite threshold and therefore no tier break at all.
    """
    points = [100.0] * 8 + [99.0, 98.0, 60.0, 59.0]
    return pd.DataFrame({
        "player_id": [f"TE{i:02d}" for i in range(len(points))],
        "position": "TE",
        "proj_points": points,
    })


def test_tied_players_do_not_set_the_positions_typical_gap():
    """`_assign_tiers`' docstring spends a paragraph arguing the median must
    be taken over NONZERO gaps only; the reviewer found both that claim and
    the median-vs-mean claim unpinned by any test. Folding the ties in here
    drives the median to 0, the threshold to infinity, and the real 38-point
    cliff out of existence."""
    out = add_vor(tie_heavy_table(), CONFIG_OBJ, teams=10)
    ranked = out.sort_values("proj_points", ascending=False).reset_index(drop=True)
    assert ranked["tier"].nunique() == 2
    assert (ranked["tier"].head(10) == 1.0).all()
    assert (ranked["tier"].tail(2) == 2.0).all()


# --- F6: a NaN proj_points must not take the whole position down with it ---


def table_with_one_nan_projection() -> pd.DataFrame:
    """Ten RBs, one with no projection at all. pandas sorts NaN LAST, so the
    NaN row lands at or past the replacement rank, `_replacement_points`
    reads it, and `proj_points - NaN` then NaNs every player at the
    position. Not reachable from `project_players` today, but `add_vor` takes
    an arbitrary projections frame and design 3.1's ADP market blend feeds
    this table next -- which is exactly where a NaN would enter."""
    return pd.DataFrame({
        "player_id": [f"RB{i}" for i in range(10)],
        "position": "RB",
        "proj_points": [100.0, 90.0, 80.0, 70.0, 60.0,
                        50.0, 40.0, 30.0, 20.0, float("nan")],
    })


def test_one_missing_projection_does_not_nan_the_whole_position():
    out = add_vor(table_with_one_nan_projection(), CONFIG_OBJ, teams=4)
    projected = out[out["proj_points"].notna()]
    assert projected["vor"].notna().all()
    # The replacement level (rank 10 at teams=4, beyond the 9 real players)
    # falls back to the worst PROJECTED player, so his VOR is exactly 0 and
    # the best player's is the full spread above him.
    assert projected.set_index("player_id").loc["RB8", "vor"] == pytest.approx(0.0)
    assert projected.set_index("player_id").loc["RB0", "vor"] == pytest.approx(80.0)


def test_a_row_with_no_projection_gets_no_vor_and_no_tier():
    # add_vor's docstring promises rows with no meaningful replacement level
    # get NaN, and sorts them last; a NaN projection previously got tier 1.0,
    # putting an unprojectable player at the TOP of a tier-sorted board.
    out = add_vor(table_with_one_nan_projection(), CONFIG_OBJ, teams=4)
    missing = out[out["proj_points"].isna()].iloc[0]
    assert pd.isna(missing["vor"])
    assert pd.isna(missing["tier"])


def test_replacement_levels_rejects_a_team_count_that_cannot_draft():
    # teams=0 previously put every position at rank 0, which add_vor skipped,
    # handing the caller a board with no VOR anywhere and no error at all --
    # inconsistent with this module's own insistence that `teams` is too
    # consequential to default.
    with pytest.raises(ValueError, match="teams"):
        replacement_levels(CONFIG_OBJ, teams=0)
    with pytest.raises(ValueError, match="teams"):
        replacement_levels(CONFIG_OBJ, teams=-1)
