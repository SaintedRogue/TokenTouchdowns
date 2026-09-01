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


def negative_rushing_history():
    """A player whose net rushing yards are negative (kneel-downs / tackles
    for loss outpacing positive gains). The sole player at this position, so
    the positional prior equals their own (negative) observed rate and
    shrinkage alone cannot pull the estimate positive -- reproducing the real
    crash this fixture guards against: an unclamped negative shrunk
    yards-per-carry rate reaching simulate_components as a negative Gamma
    scale (ValueError: scale < 0). Position is "RB" (a PROJECTABLE_POSITIONS
    member), not the fullback slot fantasy rosters actually use for this
    archetype -- FB isn't projected at all since fix round 3, and this
    fixture's whole point is a projectable player with negative efficiency.
    """
    rows = []
    for season in (2024, 2025):
        for week in range(1, 18):
            rows.append({"player_id": "C", "season": season, "week": week,
                         "position": "RB", "carries": 5, "targets": 0,
                         "receptions": 0, "rushing_yards": -10, "receiving_yards": 0,
                         "rushing_tds": 0.0, "receiving_tds": 0.0})
    return pd.DataFrame(rows)


def test_project_players_clamps_negative_efficiency_instead_of_crashing():
    out = project_players(negative_rushing_history(), CONFIG_OBJ, seasons=(2024, 2025))
    row = out.set_index("player_id").loc["C"]
    assert row["proj_points"] >= 0.0
    assert row["p10"] >= 0.0
    assert row["p90"] >= 0.0


def qb_history():
    """Two QBs with IDENTICAL passing volume and efficiency, differing only
    in rushing -- RUNNER carries far more, and more efficiently, than
    POCKET. Isolates the rushing stream's contribution to a QB projection."""
    rows = []
    for season in (2024, 2025):
        for week in range(1, 18):
            rows.append({"player_id": "POCKET", "season": season, "week": week,
                         "position": "QB", "carries": 2, "targets": 0, "receptions": 0,
                         "rushing_yards": 5, "receiving_yards": 0,
                         "rushing_tds": 0.0, "receiving_tds": 0.0,
                         "attempts": 35, "passing_yards": 260,
                         "passing_tds": 2.0, "passing_interceptions": 0.7})
            rows.append({"player_id": "RUNNER", "season": season, "week": week,
                         "position": "QB", "carries": 10, "targets": 0, "receptions": 0,
                         "rushing_yards": 55, "receiving_yards": 0,
                         "rushing_tds": 0.5, "receiving_tds": 0.0,
                         "attempts": 35, "passing_yards": 260,
                         "passing_tds": 2.0, "passing_interceptions": 0.7})
    return pd.DataFrame(rows)


def test_project_players_gives_a_running_qb_more_than_a_pocket_passer():
    # Same passing volume and efficiency for both -- only rushing differs.
    # Rushing QBs matter enormously in fantasy; the third (passing) stream
    # existing must not drown out that difference.
    out = project_players(qb_history(), CONFIG_OBJ, seasons=(2024, 2025))
    proj = out.set_index("player_id")
    assert proj.loc["RUNNER", "proj_points"] > proj.loc["POCKET", "proj_points"]


def easton_stick_shape_history():
    """Mirrors the real shape that motivated expected-games projection: a QB
    depth chart with one full-time STARTER, a few typical short-appearance
    BACKUPs (so the positional games prior reflects a realistic depth chart,
    not just full-time players), and OLD -- a player whose ENTIRE sample is a
    single old 5-game stretch with nothing since. Every player shares the
    IDENTICAL per-game passing/rushing rate (`RATE`), so any points gap
    between STARTER and OLD traces to games projection alone, not volume or
    efficiency.
    """
    RATE = dict(attempts=30, passing_yards=220, passing_tds=1.5, passing_interceptions=0.6,
                carries=3, rushing_yards=10, rushing_tds=0.0,
                targets=0, receptions=0, receiving_yards=0, receiving_tds=0.0)
    rows = []
    for season in (2024, 2025):
        for week in range(1, 18):
            rows.append({"player_id": "STARTER", "season": season, "week": week, "position": "QB", **RATE})
    for player_id, season, n_games in [("BACKUP1", 2025, 2), ("BACKUP2", 2024, 3), ("BACKUP3", 2025, 1)]:
        for week in range(1, n_games + 1):
            rows.append({"player_id": player_id, "season": season, "week": week, "position": "QB", **RATE})
    for week in range(1, 6):
        rows.append({"player_id": "OLD", "season": 2023, "week": week, "position": "QB", **RATE})
    return pd.DataFrame(rows)


def test_season_volume_projects_more_games_for_a_recent_starter_than_an_old_short_stint():
    # Ordering only, per the fix brief -- not exact values.
    out = season_volume(easton_stick_shape_history(), seasons=(2023, 2024, 2025))
    proj_games = out.set_index("player_id")["proj_games"]
    assert proj_games["STARTER"] > proj_games["OLD"]


def test_easton_stick_regression_old_sample_player_gets_a_small_fraction_of_the_points():
    # THE Easton Stick regression guard: a career backup whose entire sample
    # is one old 5-game stretch, with an IDENTICAL per-game rate to a current
    # full-time starter, must not project anywhere near a full season's
    # worth of points -- previously `games=17` was assumed for everyone, so
    # this player's hot 5-game stretch alone produced a starter-sized season
    # projection. Games projection must now do the work rates alone cannot.
    out = project_players(
        easton_stick_shape_history(), CONFIG_OBJ, seasons=(2023, 2024, 2025), seed=1,
    )
    proj = out.set_index("player_id")
    assert proj.loc["OLD", "proj_points"] < 0.6 * proj.loc["STARTER", "proj_points"]


def test_project_players_explicit_games_overrides_the_projection():
    # Pins the pre-existing override behaviour: an explicit `games=` still
    # applies flatly to every player, exactly as it did before proj_games
    # existed, regardless of their own individual history.
    out = project_players(history(), CONFIG_OBJ, seasons=(2024, 2025), games=5)
    assert (out["proj_games"] == 5.0).all()


def overplayed_history():
    """A player logged with MORE weeks than a real regular season has (a
    data quirk, or postseason weeks slipping through) -- proj_games must
    still cap at the real season length regardless of what the raw data or
    shrinkage arithmetic would otherwise produce."""
    rows = []
    for week in range(1, 21):
        rows.append({"player_id": "D", "season": 2025, "week": week,
                     "position": "WR", "carries": 0, "targets": 5,
                     "receptions": 3, "rushing_yards": 0, "receiving_yards": 40,
                     "rushing_tds": 0.0, "receiving_tds": 0.1})
    return pd.DataFrame(rows)


def test_proj_games_never_exceeds_the_real_season_length():
    out = season_volume(overplayed_history(), seasons=(2025,))
    assert out.set_index("player_id").loc["D", "proj_games"] <= 17.0


def fumble_prone_history():
    """A player with a high, sole-occupant-of-position fumble rate -- like
    negative_rushing_history, the positional prior equals their own rate,
    guaranteeing a meaningfully positive shrunk fumble rate regardless of
    strength.
    """
    rows = []
    for season in (2024, 2025):
        for week in range(1, 18):
            rows.append({"player_id": "FUMBLER", "season": season, "week": week,
                         "position": "RB", "carries": 20, "targets": 0, "receptions": 0,
                         "rushing_yards": 80, "receiving_yards": 0,
                         "rushing_tds": 0.3, "receiving_tds": 0.0,
                         "rushing_fumbles_lost": 1.0, "receiving_fumbles_lost": 0.0,
                         "sack_fumbles_lost": 0.0})
    return pd.DataFrame(rows)


def test_project_players_scores_fumbles_lost_from_league_weights():
    # THE fumbles regression guard: league.scoring_weights() has always
    # correctly derived rushing/receiving/sack_fumbles_lost weights (Fum Lost
    # = -2 in this league); the point formula previously never looked at
    # them, so every projection was silently overstated by the player's
    # fumble rate. A league that scores fumbles at -2 must project this
    # fumble-prone player LOWER than the same league scoring them at 0.
    scored = project_players(fumble_prone_history(), CONFIG_OBJ, seasons=(2024, 2025))
    no_fumble_penalty_cfg = replace_scoring(CONFIG_OBJ, {"Fum Lost": 0.0})
    unscored = project_players(fumble_prone_history(), no_fumble_penalty_cfg, seasons=(2024, 2025))
    assert scored.set_index("player_id").loc["FUMBLER", "proj_points"] < \
           unscored.set_index("player_id").loc["FUMBLER", "proj_points"]


def test_project_players_raises_when_league_scores_a_stat_it_cannot_simulate(monkeypatch):
    # Fail loud, not silent: this is the general guard the fumbles bug
    # motivated. scoring_weights() itself only ever emits the closed set of
    # columns projections.py now simulates (all 11), so this patches it to
    # simulate what happens if league.py's STAT_COLUMNS ever grows a stat
    # this module hasn't been taught to project -- exercising the real,
    # shipped `project_players` code path rather than a private helper in
    # isolation.
    import tt.projections as projections_module
    monkeypatch.setattr(
        projections_module, "scoring_weights", lambda config: {"made_up_stat": 3.0}
    )
    with pytest.raises(ValueError, match="made_up_stat"):
        project_players(history(), CONFIG_OBJ, seasons=(2024, 2025))


def kicker_and_defense_history():
    """A kicker and a team defense mixed in alongside a normal skill-position
    player -- project_players must exclude both from its output rather than
    fabricate a projection from their (meaningless, for these positions)
    offensive columns."""
    rows = []
    for season in (2024, 2025):
        for week in range(1, 18):
            rows.append({"player_id": "A", "season": season, "week": week,
                         "position": "RB", "carries": 18, "targets": 3,
                         "receptions": 2, "rushing_yards": 80, "receiving_yards": 15,
                         "rushing_tds": 0.5, "receiving_tds": 0.0})
            rows.append({"player_id": "KICKER", "season": season, "week": week,
                         "position": "K", "carries": 0, "targets": 0, "receptions": 0,
                         "rushing_yards": 0, "receiving_yards": 0,
                         "rushing_tds": 0.0, "receiving_tds": 0.0})
            rows.append({"player_id": "TEAMDEF", "season": season, "week": week,
                         "position": "DEF", "carries": 0, "targets": 0, "receptions": 0,
                         "rushing_yards": 0, "receiving_yards": 0,
                         "rushing_tds": 0.0, "receiving_tds": 0.0})
    return pd.DataFrame(rows)


def test_project_players_excludes_kickers_and_team_defenses():
    out = project_players(kicker_and_defense_history(), CONFIG_OBJ, seasons=(2024, 2025))
    assert "KICKER" not in set(out["player_id"])
    assert "TEAMDEF" not in set(out["player_id"])
    assert set(out["position"]) <= {"QB", "RB", "WR", "TE"}


def test_projectable_positions_names_the_excluded_positions_deliberately():
    from tt.projections import PROJECTABLE_POSITIONS
    assert PROJECTABLE_POSITIONS == {"QB", "RB", "WR", "TE"}
    assert "K" not in PROJECTABLE_POSITIONS
    assert "DEF" not in PROJECTABLE_POSITIONS
