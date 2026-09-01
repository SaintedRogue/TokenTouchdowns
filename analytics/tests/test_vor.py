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
