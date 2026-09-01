"""Season projections: volume is predicted, efficiency/TD rate are shrunk to
a positional prior, points are composed -- never predicted directly. See
`tt.projections` module docstring for the reasoning; these tests pin the
observable behaviour that reasoning must produce.
"""
from dataclasses import replace

import pandas as pd
import pytest

from tt.league import LeagueConfig, load_config_from_dict
from tt.projections import project_players, season_volume

# Minimal league shape matching the real export (see tt.league module
# docstring): scoring is a LIST keyed by Yahoo stat id, not display name.
# Half-PPR: Rec (statId 11) = 0.5.
CONFIG = {
    "leagueKey": "470.l.1433971", "name": "Test League",
    "numTeams": 4, "maxTeams": 10, "draftStatus": "predraft",
    "rosterSlots": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1},
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


def replace_scoring(config: LeagueConfig, overrides: dict[str, float]) -> LeagueConfig:
    """A copy of `config` with named scoring entries' values overridden, e.g.
    {"Rec": 1.0} to turn this half-PPR config into full PPR. Matches on the
    Yahoo display `name` (test-data convenience only -- scoring_weights()
    itself keys by statId, per tt.league's module docstring)."""
    new_scoring = [
        {**entry, "value": overrides[entry["name"]]} if entry["name"] in overrides else entry
        for entry in config.scoring
    ]
    return replace(config, scoring=new_scoring)


def history():
    """Two players, two seasons, stable usage."""
    rows = []
    for season in (2024, 2025):
        for week in range(1, 18):
            rows.append({"player_id": "A", "season": season, "week": week,
                         "position": "RB", "carries": 18, "targets": 3,
                         "receptions": 2, "rushing_yards": 80, "receiving_yards": 15,
                         "rushing_tds": 0.5, "receiving_tds": 0.0})
            rows.append({"player_id": "B", "season": season, "week": week,
                         "position": "WR", "carries": 0, "targets": 9,
                         "receptions": 6, "rushing_yards": 0, "receiving_yards": 75,
                         "rushing_tds": 0.0, "receiving_tds": 0.5})
    return pd.DataFrame(rows)


def test_season_volume_weights_recent_seasons_more_heavily():
    # A player whose usage doubled last season should project above the flat
    # average of both seasons.
    h = history()
    h.loc[(h.player_id == "A") & (h.season == 2025), "carries"] = 36
    out = season_volume(h, seasons=(2024, 2025), recency_weights={2024: 1.0, 2025: 3.0})
    carries = out.set_index("player_id").loc["A", "carries_per_game"]
    assert carries > 27.0   # flat mean would be 27
    assert carries < 36.0   # and never exceeds the most recent season


def test_season_volume_returns_one_row_per_player():
    out = season_volume(history(), seasons=(2024, 2025))
    assert sorted(out["player_id"]) == ["A", "B"]


def test_project_players_produces_a_distribution_not_just_a_mean():
    out = project_players(history(), CONFIG_OBJ, seasons=(2024, 2025))
    row = out.set_index("player_id").loc["A"]
    assert row["p10"] < row["proj_points"] < row["p90"]


def test_project_players_scales_with_the_number_of_games():
    short = project_players(history(), CONFIG_OBJ, seasons=(2024, 2025), games=1)
    full = project_players(history(), CONFIG_OBJ, seasons=(2024, 2025), games=17)
    assert full.set_index("player_id").loc["A", "proj_points"] > \
           10 * short.set_index("player_id").loc["A", "proj_points"]


def test_project_players_uses_league_scoring_not_a_constant():
    # A league that scores receptions at 1.0 must value a target-heavy WR more
    # than a half-PPR league does.
    half = project_players(history(), CONFIG_OBJ, seasons=(2024, 2025))
    ppr_cfg = replace_scoring(CONFIG_OBJ, {"Rec": 1.0})
    full_ppr = project_players(history(), ppr_cfg, seasons=(2024, 2025))
    assert full_ppr.set_index("player_id").loc["B", "proj_points"] > \
           half.set_index("player_id").loc["B", "proj_points"]
