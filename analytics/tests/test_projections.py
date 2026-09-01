"""Season projections: volume is predicted, efficiency/TD rate are shrunk to
a positional prior, points are composed -- never predicted directly. See
`tt.projections` module docstring for the reasoning; these tests pin the
observable behaviour that reasoning must produce.
"""
from dataclasses import replace

import pandas as pd
import pytest

from tt.league import LeagueConfig, load_config_from_dict, scoring_weights
from tt.projections import NoProjectableDataError, project_players, season_volume

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


def test_project_players_default_sample_size_gives_a_stable_spread_estimate():
    # `n` defaults to 5000 -- large enough that the reported spread (`sd`)
    # is a genuine estimate of the underlying distribution, not an artifact
    # of which handful of samples got drawn. Pins the DEFAULT specifically
    # (no `n=` passed): the weaker check above (p10 < mean < p90) passes
    # even at n=2, since two distinct samples almost always interpolate to
    # three distinct values -- it can't tell "a real distribution" from
    # "barely enough points to draw a line". Re-running the SAME projection
    # under different seeds must agree closely on `sd`; at n=2, `sd` swings
    # wildly seed to seed (measured: ~3-10 at n=2 vs ~22-23, spread <1, at
    # the real default across the same five seeds).
    sds = [
        project_players(history(), CONFIG_OBJ, seasons=(2024, 2025), seed=s)
        .set_index("player_id").loc["A", "sd"]
        for s in (1, 2, 3, 4, 5)
    ]
    assert max(sds) - min(sds) < 3.0, (
        f"sd swung {max(sds) - min(sds):.2f} across seeds -- the default "
        "sample size is too small for a stable spread estimate"
    )


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
    # `C` is the sole occupant of RB, so the positional prior equals their
    # own negative rate and can't pull the estimate positive on its own --
    # rush_eff clamps to exactly 0.0, and with zero receiving/passing volume
    # too, every one of these values is exactly 0.0, not merely non-negative.
    # `>= 0.0` against a value that IS 0.0 carries no information (it would
    # pass just as well if the clamp silently produced -5.0 rounded up to
    # "still technically doesn't crash") -- assert the actual clamped value.
    out = project_players(negative_rushing_history(), CONFIG_OBJ, seasons=(2024, 2025))
    row = out.set_index("player_id").loc["C"]
    assert row["proj_points"] == 0.0
    assert row["p10"] == 0.0
    assert row["p90"] == 0.0


def qb_history():
    """Two QBs, differing in BOTH streams: POCKET is a true pocket passer
    (zero rushing at all -- his whole projection has to come from passing);
    RUNNER throws for noticeably LESS than POCKET but makes it up, and then
    some, on the ground. Unlike an earlier version of this fixture (which
    gave both IDENTICAL passing), passing volume here actually matters to
    the RUNNER-over-POCKET comparison: zeroing the passing stream entirely
    would crater POCKET to ~0 (see the dedicated passing-stream assertion
    below) while leaving RUNNER's rushing points untouched, so the fixture
    no longer lets a "delete passing" bug hide behind two equal, cancelling
    numbers (see F9 in fix-round-1-brief.md).
    """
    rows = []
    for season in (2024, 2025):
        for week in range(1, 18):
            rows.append({"player_id": "POCKET", "season": season, "week": week,
                         "position": "QB", "carries": 0, "targets": 0, "receptions": 0,
                         "rushing_yards": 0, "receiving_yards": 0,
                         "rushing_tds": 0.0, "receiving_tds": 0.0,
                         "attempts": 38, "passing_yards": 290,
                         "passing_tds": 2.2, "passing_interceptions": 0.6})
            rows.append({"player_id": "RUNNER", "season": season, "week": week,
                         "position": "QB", "carries": 10, "targets": 0, "receptions": 0,
                         "rushing_yards": 55, "receiving_yards": 0,
                         "rushing_tds": 0.5, "receiving_tds": 0.0,
                         "attempts": 28, "passing_yards": 200,
                         "passing_tds": 1.4, "passing_interceptions": 0.7})
    return pd.DataFrame(rows)


def test_project_players_gives_a_running_qb_more_than_a_pocket_passer():
    # RUNNER throws for meaningfully less than POCKET but makes up the gap,
    # and then some, on the ground -- rushing QBs matter enormously in
    # fantasy, and that edge must survive even though POCKET out-throws him.
    out = project_players(qb_history(), CONFIG_OBJ, seasons=(2024, 2025))
    proj = out.set_index("player_id")
    assert proj.loc["RUNNER", "proj_points"] > proj.loc["POCKET", "proj_points"]

    # And the passing stream itself must actually be counted: POCKET has NO
    # rushing/receiving at all, so his entire projection is passing -- a
    # bug that deletes the passing stream (e.g. zeroing pass_volume before
    # it reaches simulate_components) collapses him to ~0, which the
    # comparison above alone cannot detect (see F9: the previous identical-
    # passing fixture let a deleted passing stream cancel out unnoticed).
    assert proj.loc["POCKET", "proj_points"] > 100.0


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


def test_project_players_warns_when_the_league_scores_ret_td_or_two_pt(recwarn):
    # F7: league.py used to drop 'Ret TD'/'2-PT' before project_players'
    # own scoring guard could ever see them -- a silent gap behind a "fails
    # loud" promise. Must now warn (not raise -- these have no simulated
    # component to add, unlike a genuine wiring bug, and the real league
    # this project targets scores both).
    cfg_with_gap = replace(
        CONFIG_OBJ,
        scoring=[
            *CONFIG_OBJ.scoring,
            {"statId": 15, "name": "Ret TD", "group": "return", "value": 6},
            {"statId": 16, "name": "2-PT", "group": "misc", "value": 2},
        ],
    )
    project_players(history(), cfg_with_gap, seasons=(2024, 2025))
    messages = [str(w.message) for w in recwarn.list]
    assert any("Ret TD" in m and "2-PT" in m for m in messages)


def test_project_players_does_not_warn_for_a_fully_mapped_league(recwarn):
    project_players(history(), CONFIG_OBJ, seasons=(2024, 2025))
    assert len(recwarn.list) == 0


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


def kicker_only_history():
    """No PROJECTABLE_POSITIONS rows at all -- unlike
    kicker_and_defense_history(), nothing survives the position filter, so
    `subset` reaching `_positional_priors` is genuinely empty."""
    rows = []
    for season in (2024, 2025):
        for week in range(1, 18):
            rows.append({"player_id": "KICKER", "season": season, "week": week,
                         "position": "K", "carries": 0, "targets": 0, "receptions": 0,
                         "rushing_yards": 0, "receiving_yards": 0,
                         "rushing_tds": 0.0, "receiving_tds": 0.0})
    return pd.DataFrame(rows)


def test_project_players_raises_a_named_error_on_a_kicker_only_frame():
    # F8: this used to die with `KeyError: "None of ['position'] are in the
    # columns"` -- correct that there's nothing to compute, useless as a
    # message about WHY.
    with pytest.raises(NoProjectableDataError):
        project_players(kicker_only_history(), CONFIG_OBJ, seasons=(2024, 2025))


def test_project_players_raises_a_named_error_when_no_season_matches():
    # F8: a seasons= window matching zero rows (e.g. a pre-season call
    # before any games in that season exist) hits the same empty-subset
    # path as the kicker-only frame above.
    with pytest.raises(NoProjectableDataError):
        project_players(history(), CONFIG_OBJ, seasons=(2099,))


def test_projectable_positions_names_the_excluded_positions_deliberately():
    from tt.projections import PROJECTABLE_POSITIONS
    assert PROJECTABLE_POSITIONS == {"QB", "RB", "WR", "TE"}
    assert "K" not in PROJECTABLE_POSITIONS
    assert "DEF" not in PROJECTABLE_POSITIONS


def history_with_display_name():
    """The real nflverse history carries player_display_name -- a board keyed
    on nflverse ids like 00-0030506 is unreadable at a live draft with 60
    seconds per pick (see module docstring)."""
    h = history()
    h["player_display_name"] = h["player_id"].map({"A": "Aaron Aback", "B": "Bobby Byrne"})
    return h


def test_project_players_carries_player_display_name_through_as_name():
    out = project_players(history_with_display_name(), CONFIG_OBJ, seasons=(2024, 2025))
    names = out.set_index("player_id")["name"]
    assert names["A"] == "Aaron Aback"
    assert names["B"] == "Bobby Byrne"


def history_with_player_name_only():
    """Some nflverse sources carry only the shorter player_name, not
    player_display_name -- the fallback this history fixture exercises."""
    h = history()
    h["player_name"] = h["player_id"].map({"A": "A.Aback", "B": "B.Byrne"})
    return h


def test_project_players_falls_back_to_player_name_when_display_name_is_absent():
    out = project_players(history_with_player_name_only(), CONFIG_OBJ, seasons=(2024, 2025))
    names = out.set_index("player_id")["name"]
    assert names["A"] == "A.Aback"
    assert names["B"] == "B.Byrne"


def test_project_players_prefers_display_name_over_player_name_when_both_present():
    h = history()
    h["player_display_name"] = h["player_id"].map({"A": "Aaron Aback", "B": "Bobby Byrne"})
    h["player_name"] = h["player_id"].map({"A": "A.Aback", "B": "B.Byrne"})
    out = project_players(h, CONFIG_OBJ, seasons=(2024, 2025))
    names = out.set_index("player_id")["name"]
    assert names["A"] == "Aaron Aback"
    assert names["B"] == "Bobby Byrne"


def test_project_players_falls_back_to_player_id_when_no_name_column_exists():
    # Every other fixture in this file carries no name column at all -- the
    # least-wrong fallback is the player_id itself, not a missing column or
    # a NaN that would break a board's display.
    out = project_players(history(), CONFIG_OBJ, seasons=(2024, 2025))
    names = out.set_index("player_id")["name"]
    assert names["A"] == "A"
    assert names["B"] == "B"


def test_project_players_treats_an_empty_display_name_as_missing():
    # `fillna` alone leaves "" in place (it isn't NaN) -- an empty
    # player_display_name must still fall back to player_name, and an empty
    # player_name must still fall back to the player_id itself.
    h = history()
    h["player_display_name"] = h["player_id"].map({"A": "", "B": "Bobby Byrne"})
    h["player_name"] = h["player_id"].map({"A": "", "B": "B.Byrne"})
    out = project_players(h, CONFIG_OBJ, seasons=(2024, 2025))
    names = out.set_index("player_id")["name"]
    assert names["A"] == "A"  # both name columns empty -> player_id
    assert names["B"] == "Bobby Byrne"


def test_project_players_uses_the_most_recent_seasons_name_after_a_rename():
    # `history()`'s own row order lists every 2024 week (Old Name) before
    # any 2025 week (New Name) -- exactly the shape a naive
    # `groupby(...).first()` gets wrong: it would return the OLDEST name
    # ("Old Name") simply because those rows come first in the frame. Must
    # resolve to the MOST RECENT season's name regardless of row order.
    h = history()
    h["player_display_name"] = None
    h.loc[(h["player_id"] == "A") & (h["season"] == 2024), "player_display_name"] = "Old Name"
    h.loc[(h["player_id"] == "A") & (h["season"] == 2025), "player_display_name"] = "New Name"
    out = project_players(h, CONFIG_OBJ, seasons=(2024, 2025))
    assert out.set_index("player_id").loc["A", "name"] == "New Name"


# ---------------------------------------------------------------------------
# F1: postseason rows must never be counted as regular-season games.
# ---------------------------------------------------------------------------


def history_with_postseason():
    """The real nflverse `stats_player_week_{season}.parquet` shape: a
    `season_type` column in {REG, POST}, with POST rows at weeks 19-22.

    PLAYOFF is a player whose regular season is a flat 10 carries a game for
    all 17 weeks, and who then plays four postseason games at 30 carries. Only
    the 17 REG rows describe a regular-season fantasy player; the four POST
    rows both triple his apparent per-game volume and push his games-played to
    21 (which `SEASON_LENGTH` would then silently clamp to 17, hiding the
    inflation rather than preventing it). Because only good teams play in
    January, this bias is correlated with team quality -- it looks exactly
    like signal.
    """
    rows = []
    for week in range(1, 18):
        rows.append({"player_id": "PLAYOFF", "season": 2025, "week": week,
                     "season_type": "REG", "position": "RB", "carries": 10,
                     "targets": 0, "receptions": 0, "rushing_yards": 40,
                     "receiving_yards": 0, "rushing_tds": 0.0, "receiving_tds": 0.0})
    for week in range(19, 23):
        rows.append({"player_id": "PLAYOFF", "season": 2025, "week": week,
                     "season_type": "POST", "position": "RB", "carries": 30,
                     "targets": 0, "receptions": 0, "rushing_yards": 120,
                     "receiving_yards": 0, "rushing_tds": 0.0, "receiving_tds": 0.0})
    return pd.DataFrame(rows)


def test_season_volume_excludes_postseason_rows_from_per_game_volume():
    # LEVEL, not direction: the REG-only per-game rate is exactly 10.0.
    # Including the four 30-carry POST games gives 13.8.
    out = season_volume(history_with_postseason(), seasons=(2025,))
    assert out.set_index("player_id").loc["PLAYOFF", "carries_per_game"] == pytest.approx(10.0)


def test_season_volume_excludes_postseason_rows_from_games_played():
    # `games` is the raw observed count feeding the expected-games shrinkage.
    # 17 regular-season games, not 21 playoff-inflated ones (which
    # SEASON_LENGTH would clamp back to 17, concealing the inflation).
    out = season_volume(history_with_postseason(), seasons=(2025,))
    assert out.set_index("player_id").loc["PLAYOFF", "games"] == 17


def test_project_players_excludes_postseason_rows_from_efficiency_priors():
    # The postseason rows here are MORE efficient (4.0 yds/carry both, but
    # triple the volume), so leaving them in inflates the projection. Pinned
    # against the identical history with the POST rows physically removed:
    # filtering must be exactly equivalent to never having been given them.
    full = history_with_postseason()
    reg_only = full[full["season_type"] == "REG"].drop(columns=["season_type"])
    with_post = project_players(full, CONFIG_OBJ, seasons=(2025,), seed=5)
    without = project_players(reg_only, CONFIG_OBJ, seasons=(2025,), seed=5)
    assert with_post.set_index("player_id").loc["PLAYOFF", "proj_points"] == pytest.approx(
        without.set_index("player_id").loc["PLAYOFF", "proj_points"]
    )


# ---------------------------------------------------------------------------
# F2: every scoring component must be PINNED, not merely present as a dict key.
# ---------------------------------------------------------------------------


def single_stream_history():
    """One player per volume stream, each with volume in EXACTLY one stream.

    This isolation is the whole point. `_validate_scoring_is_simulated` can
    only check that `components` HAS a key -- never that the key holds the
    right array -- so both `"passing_yards": np.zeros(n)` and a mis-key like
    `"passing_yards": rush_yards` pass it. A fixture whose players accumulate
    several streams at once cannot tell those apart either: a mis-keyed
    passing weight would still move a QB who also runs.

    PASSER throws and does nothing else (zero carries, zero targets), RUSHER
    only runs, RECEIVER only catches -- so for each of the eleven scored
    components there is exactly one player for whom that component is the
    ONLY thing its array could legitimately be. Each is the sole occupant of
    their position, so the positional prior equals their own observed rate
    and every simulated rate is meaningfully positive.
    """
    rows = []
    for season in (2024, 2025):
        for week in range(1, 18):
            rows.append({"player_id": "PASSER", "season": season, "week": week,
                         "position": "QB",
                         "carries": 0, "rushing_yards": 0, "rushing_tds": 0.0,
                         "rushing_fumbles_lost": 0.0,
                         "targets": 0, "receptions": 0, "receiving_yards": 0,
                         "receiving_tds": 0.0, "receiving_fumbles_lost": 0.0,
                         "attempts": 35, "passing_yards": 260, "passing_tds": 2.0,
                         "passing_interceptions": 0.8, "sack_fumbles_lost": 0.3})
            rows.append({"player_id": "RUSHER", "season": season, "week": week,
                         "position": "RB",
                         "carries": 20, "rushing_yards": 90, "rushing_tds": 0.6,
                         "rushing_fumbles_lost": 0.3,
                         "targets": 0, "receptions": 0, "receiving_yards": 0,
                         "receiving_tds": 0.0, "receiving_fumbles_lost": 0.0,
                         "attempts": 0, "passing_yards": 0, "passing_tds": 0.0,
                         "passing_interceptions": 0.0, "sack_fumbles_lost": 0.0})
            rows.append({"player_id": "RECEIVER", "season": season, "week": week,
                         "position": "WR",
                         "carries": 0, "rushing_yards": 0, "rushing_tds": 0.0,
                         "rushing_fumbles_lost": 0.0,
                         "targets": 10, "receptions": 7, "receiving_yards": 95,
                         "receiving_tds": 0.6, "receiving_fumbles_lost": 0.3,
                         "attempts": 0, "passing_yards": 0, "passing_tds": 0.0,
                         "passing_interceptions": 0.0, "sack_fumbles_lost": 0.0})
    return pd.DataFrame(rows)


# Which single-stream player accumulates each scored component.
COMPONENT_OWNER = {
    "rushing_yards": "RUSHER", "rushing_tds": "RUSHER", "rushing_fumbles_lost": "RUSHER",
    "receiving_yards": "RECEIVER", "receiving_tds": "RECEIVER", "receptions": "RECEIVER",
    "receiving_fumbles_lost": "RECEIVER",
    "passing_yards": "PASSER", "passing_tds": "PASSER",
    "passing_interceptions": "PASSER", "sack_fumbles_lost": "PASSER",
}


def _project_with_weights(weights, monkeypatch):
    """Run the real `project_players` path with `scoring_weights` replaced.

    Patching `scoring_weights` (the same technique
    `test_project_players_raises_when_league_scores_a_stat_it_cannot_simulate`
    already uses) is what lets a test vary ONE component's weight in
    isolation: the league export keys scoring by Yahoo stat id, and id 18
    alone maps to all three fumble columns, so no config edit can isolate
    `rushing_fumbles_lost` from `sack_fumbles_lost`. The fixture's
    single-stream players supply that isolation instead.
    """
    import tt.projections as projections_module
    monkeypatch.setattr(projections_module, "scoring_weights", lambda config: dict(weights))
    return project_players(
        single_stream_history(), CONFIG_OBJ, seasons=(2024, 2025), n=400, seed=17,
    ).set_index("player_id")["proj_points"]


BASE_WEIGHTS = scoring_weights(CONFIG_OBJ)


def test_every_simulated_component_is_scored_by_the_league_weights():
    # Guards the matrix below against silently shrinking: if a component is
    # ever dropped from `scoring_weights`' reach, the parametrised test would
    # simply stop covering it and still report green.
    from tt.projections import _SIMULATED_COMPONENTS
    assert set(BASE_WEIGHTS) == set(_SIMULATED_COMPONENTS)
    assert len(BASE_WEIGHTS) == 11


@pytest.mark.parametrize("stat", sorted(BASE_WEIGHTS))
def test_each_scoring_component_actually_reaches_proj_points(stat, monkeypatch):
    """THE guard for the fumbles bug class, applied to all eleven components.

    `proj_points` is by construction LINEAR in each scoring weight
    (`points = sum(components[stat] * weight[stat])`), and every stream's RNG
    seed is derived from the player id alone, so the simulated component
    arrays are byte-identical across these three runs. That gives an exact
    algebraic identity to assert rather than a direction:

        proj(weight doubled) - proj(base) == proj(base) - proj(weight zeroed)

    and both differences equal `weight[stat] * mean(components[stat])`. If
    that component were zeroed (`np.zeros(n)`) or mis-keyed to another
    stream's array, its owner -- who has volume in NO other stream -- would
    see a difference of exactly 0, which the magnitude assertion rejects. The
    sign assertion additionally pins that a NEGATIVE weight (interceptions,
    fumbles lost) subtracts: removing a penalty must RAISE the projection.
    """
    owner = COMPONENT_OWNER[stat]
    weight = BASE_WEIGHTS[stat]

    base = _project_with_weights(BASE_WEIGHTS, monkeypatch)[owner]
    zeroed = _project_with_weights({**BASE_WEIGHTS, stat: 0.0}, monkeypatch)[owner]
    doubled = _project_with_weights({**BASE_WEIGHTS, stat: weight * 2.0}, monkeypatch)[owner]

    contribution = base - zeroed
    # `summarise` rounds proj_points to 2 dp, so the identity holds to within
    # the rounding of the three values it is computed from, not to machine
    # precision.
    assert doubled - base == pytest.approx(contribution, abs=0.05)
    assert abs(contribution) > 0.5, (
        f"{stat!r} contributes nothing to {owner}'s projection -- its entry in "
        "`components` is zeroed, mis-keyed, or never summed"
    )
    assert (contribution > 0) == (weight > 0), (
        f"{stat!r} moves {owner}'s projection in the wrong direction for a "
        f"weight of {weight}"
    )


# ---------------------------------------------------------------------------
# F3: expected-games shrinkage must not depend on how many seasons the caller
# happened to pass.
# ---------------------------------------------------------------------------


def seven_season_history():
    """One full-time IRONMAN (17 games in every season 2019-2025) alongside a
    realistic depth chart of short-stint backups in every season.

    The backups are what give the positional games prior a stable, modest
    value in EVERY window, so the only thing varying between a 2-season and a
    7-season call is the arithmetic under test -- not the prior it regresses
    toward, and not IRONMAN's own weighted mean games (a flat 17 either way).
    """
    RATE = dict(attempts=30, passing_yards=220, passing_tds=1.5,
                passing_interceptions=0.6, carries=3, rushing_yards=10,
                rushing_tds=0.0, targets=0, receptions=0, receiving_yards=0,
                receiving_tds=0.0)
    rows = []
    for season in range(2019, 2026):
        for week in range(1, 18):
            rows.append({"player_id": "IRONMAN", "season": season, "week": week,
                         "position": "QB", **RATE})
        for n_games, tag in [(2, "a"), (3, "b"), (1, "c"), (5, "d")]:
            for week in range(1, n_games + 1):
                rows.append({"player_id": f"BACKUP{tag}{season}", "season": season,
                             "week": week, "position": "QB", **RATE})
    return pd.DataFrame(rows)


def test_proj_games_does_not_depend_on_how_many_seasons_were_supplied():
    """THE F3 guard. `games_evidence` was the raw geometric sum of the recency
    weights (2 seasons -> 4, 7 seasons -> 1093) weighed against a fixed
    GAMES_STRENGTH, so the positional prior's share of the estimate slid from
    50% to 0.4% purely from how much ancient, near-zero-weight history the
    caller happened to include. Measured on real data, Josh Allen projected
    12.18 games on a 2-season window and 16.05 on a 7-season one -- a 32%
    swing in proj_points with his weighted per-game volume identical to 2 dp,
    and non-uniform across players, so it moved the ranking too.

    Normalised evidence expresses EFFECTIVE SEASONS instead, which is bounded
    regardless of window length, so the same player must land in the same
    place.
    """
    history = seven_season_history()
    two = season_volume(history, seasons=(2024, 2025))
    seven = season_volume(history, seasons=tuple(range(2019, 2026)))
    narrow = two.set_index("player_id").loc["IRONMAN", "proj_games"]
    wide = seven.set_index("player_id").loc["IRONMAN", "proj_games"]
    assert narrow == pytest.approx(wide, abs=0.5), (
        f"proj_games moved {abs(wide - narrow):.2f} games purely from widening "
        "the seasons window"
    )


def test_games_evidence_is_bounded_effective_seasons_not_a_geometric_total():
    """Pins the mechanism, not just its symptom. Under the default 3x recency
    scheme the normalised weights are 1, 1/3, 1/9, ... so a player present in
    every season of ANY window has an effective sample size in [1, 1.5) --
    bounded -- whereas the raw geometric sum grows without limit (4, 13, 121,
    1093, ...). A regression to the raw sum makes this run away."""
    from tt.projections import _games_evidence, _default_recency_weights

    for length in (2, 3, 5, 7, 12):
        seasons = tuple(range(2026 - length, 2026))
        weights = _default_recency_weights(seasons)
        evidence = _games_evidence(list(weights.values()), weights)
        assert 1.0 <= evidence < 1.5, f"{length} seasons gave evidence {evidence}"


def test_a_low_evidence_player_is_pulled_toward_the_positional_games_prior():
    """GAMES_STRENGTH itself must be pinned: the reviewer found the Easton
    Stick regression test stays green with `GAMES_STRENGTH = 0.0`, i.e. with
    the shrinkage the whole fix is about entirely disabled.

    OLD's only evidence is a single 5-game season two years back. His raw
    weighted mean is 5.0 games; the positional prior (a depth chart full of
    short-stint backups plus one ironman) is well below 17. Shrinkage must
    leave him STRICTLY BETWEEN the two: equal to 5.0 means no shrinkage at
    all (strength 0), equal to the prior means his own history was discarded
    (infinite strength). Both degenerate ends are excluded by construction.
    """
    from tt.projections import GAMES_STRENGTH, season_volume as sv
    history = easton_stick_shape_history()
    out = sv(history, seasons=(2023, 2024, 2025)).set_index("player_id")

    # The positional prior is the unweighted mean games across every
    # player-season at the position, recomputed here from the fixture itself
    # rather than hardcoded, so the test tracks the fixture.
    reg = history[history["season"].isin((2023, 2024, 2025))]
    prior = reg.groupby(["player_id", "season"])["week"].count().mean()

    old = out.loc["OLD", "proj_games"]

    # LEVEL, restated independently of the implementation. OLD's only season
    # is 2023, whose recency weight is 1 against a most-recent-season weight
    # of 9, so his effective evidence is 1/9 of a season and his normalised
    # weighted games are 5/9. The documented blend is then
    # (weighted + prior * strength) / (evidence + strength).
    evidence = 1.0 / 9.0
    expected = (5.0 * evidence + prior * GAMES_STRENGTH) / (evidence + GAMES_STRENGTH)
    assert old == pytest.approx(expected, abs=1e-9)

    # And the two degenerate ends are excluded by a real margin, not by a
    # float epsilon: GAMES_STRENGTH = 0.0 lands exactly on 5.0, an infinite
    # strength lands exactly on the prior.
    assert old > 5.0 + 1.0
    assert old < prior


def test_two_season_proj_games_is_unchanged_by_the_evidence_normalisation():
    """The default call is a 2-season window, and the normalisation is a pure
    change of UNITS there: dividing the evidence by the maximum recency weight
    (3, for two seasons) and dividing GAMES_STRENGTH by the same 3 leaves the
    blend algebraically identical. This pins that the fix removed the window
    dependence WITHOUT quietly re-tuning the default behaviour it was already
    calibrated for -- the hand-computed value below is the pre-fix output.
    """
    from tt.projections import GAMES_STRENGTH
    out = season_volume(easton_stick_shape_history(), seasons=(2024, 2025))
    starter = out.set_index("player_id").loc["STARTER", "proj_games"]

    # Hand-computed against the OLD (pre-normalisation) arithmetic:
    # weights 1 (2024) and 3 (2025); STARTER plays 17 in both, so
    # weighted_games = 1*17 + 3*17 = 68 and raw evidence = 4.
    # prior = mean games per player-season at QB over the two seasons.
    reg = easton_stick_shape_history()
    reg = reg[reg["season"].isin((2024, 2025))]
    prior = reg.groupby(["player_id", "season"])["week"].count().mean()
    expected = (68.0 + prior * 4.0) / (4.0 + 4.0)
    assert starter == pytest.approx(expected, abs=1e-9)
    # And the constant is expressed on the effective-seasons scale.
    assert GAMES_STRENGTH == pytest.approx(4.0 / 3.0)


# ---------------------------------------------------------------------------
# F9: efficiency/TD-rate shrinkage strengths are unpinned -- RUSH_EFF_STRENGTH
# = 0.0 (shrinkage entirely disabled) survives the whole suite, and the same
# is true of every other efficiency/TD-rate STRENGTH constant. Pinned the
# same way GAMES_STRENGTH was (F3, above): a fixture where the player's own
# raw rate clearly differs from the pooled positional prior, and an
# assertion that shrinkage lands the OBSERVED rate strictly inside the two,
# away from both degenerate ends (strength=0 -> exactly the raw rate;
# strength=infinity -> exactly the prior) by a real margin.
# ---------------------------------------------------------------------------

_ZERO_STREAM_ROW = dict(
    carries=0, rushing_yards=0, rushing_tds=0.0, rushing_fumbles_lost=0.0,
    targets=0, receptions=0, receiving_yards=0, receiving_tds=0.0, receiving_fumbles_lost=0.0,
    attempts=0, passing_yards=0, passing_tds=0.0, passing_interceptions=0.0, sack_fumbles_lost=0.0,
)


def two_group_history(position: str, sample: dict, other: dict, other_count: int = 6) -> pd.DataFrame:
    """SAMPLE (one player) plus `other_count` OTHER players at `position`,
    each at a CONSTANT weekly rate for every week of both seasons -- so the
    pooled positional prior (`_positional_priors` sums numerator/denominator
    across every row at the position) differs meaningfully from SAMPLE's own
    rate, letting a shrinkage-strength test observe the pull between them.
    Every row starts from `_ZERO_STREAM_ROW`, so only the one stream a given
    case overrides ever contributes to proj_points -- no other simulated
    component can contaminate the isolated stream under test."""
    rows = []
    for season in (2024, 2025):
        for week in range(1, 18):
            rows.append({"player_id": "SAMPLE", "season": season, "week": week,
                         "position": position, **_ZERO_STREAM_ROW, **sample})
            for i in range(other_count):
                rows.append({"player_id": f"OTHER{i}", "season": season, "week": week,
                             "position": position, **_ZERO_STREAM_ROW, **other})
    return pd.DataFrame(rows)


# (constant name, position, sample overrides, other overrides, the scored
# stat this stream's points come from, the season_volume per-game column
# that stream's volume lives in, sample's own raw rate, others' raw rate).
# Every case's raw rates are chosen well apart (>=3x) so shrinkage's pull is
# unambiguous against Monte Carlo noise at n=20000.
_SHRINKAGE_STRENGTH_CASES = [
    ("RUSH_EFF_STRENGTH", "RB",
     dict(carries=20, rushing_yards=100), dict(carries=20, rushing_yards=40),
     "rushing_yards", "carries_per_game", 5.0, 2.0),
    ("REC_EFF_STRENGTH", "WR",
     dict(targets=10, receiving_yards=150), dict(targets=10, receiving_yards=60),
     "receiving_yards", "targets_per_game", 15.0, 6.0),
    ("PASS_EFF_STRENGTH", "QB",
     dict(attempts=35, passing_yards=350), dict(attempts=35, passing_yards=210),
     "passing_yards", "attempts_per_game", 10.0, 6.0),
    ("RUSH_TD_STRENGTH", "RB",
     dict(carries=20, rushing_tds=2.0), dict(carries=20, rushing_tds=0.2),
     "rushing_tds", "carries_per_game", 0.1, 0.01),
    ("REC_TD_STRENGTH", "WR",
     dict(targets=10, receiving_tds=1.5), dict(targets=10, receiving_tds=0.15),
     "receiving_tds", "targets_per_game", 0.15, 0.015),
    ("PASS_TD_STRENGTH", "QB",
     dict(attempts=35, passing_tds=4.0), dict(attempts=35, passing_tds=0.7),
     "passing_tds", "attempts_per_game", 4.0 / 35, 0.7 / 35),
    ("CATCH_RATE_STRENGTH", "WR",
     dict(targets=10, receptions=9.5), dict(targets=10, receptions=3.0),
     "receptions", "targets_per_game", 0.95, 0.30),
    ("PASS_INT_STRENGTH", "QB",
     dict(attempts=35, passing_interceptions=3.0), dict(attempts=35, passing_interceptions=0.35),
     "passing_interceptions", "attempts_per_game", 3.0 / 35, 0.35 / 35),
]


@pytest.mark.parametrize(
    "strength_name,position,sample,other,weight_stat,volume_column,sample_rate,other_rate",
    _SHRINKAGE_STRENGTH_CASES,
    ids=[case[0] for case in _SHRINKAGE_STRENGTH_CASES],
)
def test_shrinkage_strength_pulls_the_observed_rate_toward_the_prior(
    strength_name, position, sample, other, weight_stat, volume_column,
    sample_rate, other_rate,
):
    history = two_group_history(position, sample, other)
    weight = scoring_weights(CONFIG_OBJ)[weight_stat]

    # season_volume gives SAMPLE's real per-game volume/proj_games exactly
    # as project_players would compute them -- reused, not hand-derived, so
    # the algebra below tracks the fixture rather than an assumption about
    # what season_volume returns for it.
    volume_row = season_volume(history, seasons=(2024, 2025)).set_index("player_id").loc["SAMPLE"]
    per_game = volume_row[volume_column]
    proj_games = volume_row["proj_games"]

    out = project_players(history, CONFIG_OBJ, seasons=(2024, 2025), n=20_000, seed=5)
    sample_points = out.set_index("player_id").loc["SAMPLE", "proj_points"]

    # Every other stream is exactly zero by construction (_ZERO_STREAM_ROW),
    # so proj_points = weight * E[count] = weight * per_game * proj_games *
    # shrunk_rate -- solved backward for the one unknown, shrunk_rate itself.
    observed_rate = sample_points / weight / (per_game * proj_games)

    # A convex combination of a convex combination of {sample_rate,
    # other_rate} is itself bounded in [other_rate, sample_rate] for ANY
    # finite positive strength -- so this margin isn't case-specific tuning,
    # it is what "meaningfully off both raw rates" means for every case.
    span = sample_rate - other_rate
    low, high = other_rate + 0.15 * span, sample_rate - 0.15 * span
    assert low < observed_rate < high, (
        f"{strength_name}: observed rate {observed_rate:.5f} is not "
        f"meaningfully shrunk between the raw rates ({other_rate}..{sample_rate}) "
        f"-- expected inside ({low:.5f}, {high:.5f})"
    )
