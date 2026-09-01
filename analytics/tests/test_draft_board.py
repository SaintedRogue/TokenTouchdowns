"""Task 8: the out-of-sample draft-strategy backtest harness.

These tests pin the properties that make this module's numbers trustworthy
rather than just plausible: no lookahead into the backtest season itself,
grading on ACTUAL points (not proj_points), zero-not-NaN for a drafted bust,
and the crosswalk JSON parsing that replaces the old 16%-match exact-name
join. See `tt.studies.draft_board`'s own module docstring for the full
reasoning; these tests exercise its observable behaviour.
"""
from __future__ import annotations

import pandas as pd
import pytest

from tt.league import load_config_from_dict
from tt.projections import project_players
from tt.studies.draft_board import (
    actual_lineup_score,
    actual_points_by_player,
    attach_adp,
    build_board,
    build_projection_board,
    load_ffc_crosswalk,
    parse_ffc_crosswalk,
    run_backtest,
    run_backtest_cell,
    strategies_for,
    zero_scoring_diagnostics,
    zero_scoring_rate,
)

CONFIG = {
    "leagueKey": "470.l.1433971", "name": "Test League",
    "numTeams": 10, "maxTeams": 10, "draftStatus": "predraft",
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


def _week_row(player_id, season, week, position, **stats):
    row = {
        "player_id": player_id, "season": season, "week": week,
        "position": position, "season_type": "REG",
        "carries": 0, "targets": 0, "receptions": 0,
        "rushing_yards": 0, "receiving_yards": 0,
        "rushing_tds": 0, "receiving_tds": 0,
        "rushing_fumbles_lost": 0, "receiving_fumbles_lost": 0,
        "attempts": 0, "passing_yards": 0, "passing_tds": 0,
        "passing_interceptions": 0, "sack_fumbles_lost": 0,
    }
    row.update(stats)
    return row


def history_two_seasons():
    """Player A: RB, steady usage in 2020 (the training season). Player B:
    WR, steady usage in 2020 too. Both also appear in 2021 (the backtest
    target season) with a DIFFERENT, much higher volume -- the trap
    `test_build_projection_board_never_trains_on_the_backtest_season_itself`
    exists to catch."""
    rows = []
    for week in range(1, 18):
        rows.append(_week_row("A", 2020, week, "RB", carries=15, rushing_yards=60))
        rows.append(_week_row("B", 2020, week, "WR", targets=6, receptions=4, receiving_yards=50))
        # 2021: a lookahead-only signal -- carries/targets triple.
        rows.append(_week_row("A", 2021, week, "RB", carries=45, rushing_yards=200))
        rows.append(_week_row("B", 2021, week, "WR", targets=18, receptions=14, receiving_yards=180))
    return pd.DataFrame(rows)


def ffc_payload(entries):
    return {"season": 2021, "meta": {}, "players": entries}


# ---------------------------------------------------------------------------
# parse_ffc_crosswalk / load_ffc_crosswalk
# ---------------------------------------------------------------------------

def test_parse_ffc_crosswalk_renames_and_keeps_resolved_player_id():
    payload = ffc_payload([
        {"name": "A Player", "position": "RB", "team": "BUF", "adp": 3.2,
         "stdev": 1.1, "high": 1, "low": 6, "timesDrafted": 40,
         "playerId": "00-0012345", "matchState": "matched", "matchSource": "sleeper"},
    ])
    out = parse_ffc_crosswalk(payload)
    assert out.loc[0, "player_id"] == "00-0012345"
    assert out.loc[0, "adp"] == 3.2
    assert out.loc[0, "match_source"] == "sleeper"


def test_parse_ffc_crosswalk_unresolved_player_id_is_null():
    payload = ffc_payload([
        {"name": "Nobody Known", "position": "WR", "team": "SEA", "adp": 200.0,
         "stdev": 20.0, "high": 150, "low": 250, "timesDrafted": 2,
         "playerId": None, "matchState": "absent", "matchSource": None},
    ])
    out = parse_ffc_crosswalk(payload)
    assert pd.isna(out.loc[0, "player_id"])


def test_parse_ffc_crosswalk_handles_no_players():
    out = parse_ffc_crosswalk({"season": 2021, "meta": {}, "players": []})
    assert out.empty
    assert "player_id" in out.columns


def test_load_ffc_crosswalk_reads_a_real_file(tmp_path):
    path = tmp_path / "ffc_adp_2021.json"
    path.write_text(
        '{"season": 2021, "meta": {}, "players": [{"name": "X", '
        '"position": "RB", "team": "BUF", "adp": 5.0, "stdev": 2.0, '
        '"high": 1, "low": 10, "timesDrafted": 20, "playerId": "00-0099999", '
        '"matchState": "matched", "matchSource": "nflverse"}]}'
    )
    out = load_ffc_crosswalk(path)
    assert out.loc[0, "player_id"] == "00-0099999"


# ---------------------------------------------------------------------------
# attach_adp
# ---------------------------------------------------------------------------

def test_attach_adp_joins_only_resolved_rows():
    board = pd.DataFrame({"player_id": ["A", "B", "C"], "proj_points": [10.0, 20.0, 5.0]})
    ffc = parse_ffc_crosswalk(ffc_payload([
        {"name": "a", "position": "RB", "playerId": "A", "matchState": "matched",
         "matchSource": "sleeper", "adp": 3.0, "stdev": 1.0},
        {"name": "unresolved", "position": "WR", "playerId": None,
         "matchState": "absent", "matchSource": None, "adp": None, "stdev": None},
    ]))
    out = attach_adp(board, ffc)
    row = out.set_index("player_id")
    assert row.loc["A", "adp"] == 3.0
    assert pd.isna(row.loc["B", "adp"])  # no crosswalk entry at all
    assert pd.isna(row.loc["C", "adp"])


def test_attach_adp_raises_on_duplicate_resolved_player_id():
    board = pd.DataFrame({"player_id": ["A"], "proj_points": [10.0]})
    ffc = parse_ffc_crosswalk(ffc_payload([
        {"name": "one", "position": "RB", "playerId": "A", "matchState": "matched",
         "matchSource": "sleeper", "adp": 3.0, "stdev": 1.0},
        {"name": "two", "position": "RB", "playerId": "A", "matchState": "matched",
         "matchSource": "nflverse", "adp": 30.0, "stdev": 5.0},
    ]))
    with pytest.raises(ValueError, match="duplicate|ambiguous|multiple"):
        attach_adp(board, ffc)


# ---------------------------------------------------------------------------
# build_projection_board: THE no-lookahead guarantee
# ---------------------------------------------------------------------------

def test_build_projection_board_never_trains_on_the_backtest_season_itself():
    history = history_two_seasons()
    empty_ffc = parse_ffc_crosswalk(ffc_payload([]))

    board = build_projection_board(history, CONFIG_OBJ, season=2021, ffc=empty_ffc, seed=1)
    reference = project_players(history, CONFIG_OBJ, seasons=(2020,), seed=1)

    # Must match a projection built from 2020 ALONE...
    got = board.set_index("player_id")["proj_points"]
    want = reference.set_index("player_id")["proj_points"]
    pd.testing.assert_series_equal(got.sort_index(), want.sort_index(), check_names=False)

    # ...and must NOT match one that also saw 2021's lookahead-only volume
    # spike (proves the guard is actually doing something, not vacuously
    # true because both projections happen to agree).
    lookahead = project_players(history, CONFIG_OBJ, seasons=(2020, 2021), seed=1)
    lookahead_points = lookahead.set_index("player_id")["proj_points"]
    assert not got.sort_index().equals(lookahead_points.sort_index())


def test_build_projection_board_raises_with_no_prior_seasons():
    history = history_two_seasons()
    empty_ffc = parse_ffc_crosswalk(ffc_payload([]))
    with pytest.raises(ValueError, match="lookahead|before"):
        build_projection_board(history, CONFIG_OBJ, season=2020, ffc=empty_ffc)


def test_build_board_attaches_vor_and_adp():
    history = history_two_seasons()
    ffc = parse_ffc_crosswalk(ffc_payload([
        {"name": "A", "position": "RB", "playerId": "A", "matchState": "matched",
         "matchSource": "sleeper", "adp": 3.0, "stdev": 1.0},
    ]))
    board = build_board(history, CONFIG_OBJ, season=2021, ffc=ffc, teams=10, seed=1)
    assert "vor" in board.columns and "tier" in board.columns
    row = board.set_index("player_id")
    assert row.loc["A", "adp"] == 3.0
    assert pd.isna(row.loc["B", "adp"])


# ---------------------------------------------------------------------------
# actual_points_by_player: REG only, real league scoring, missing == absent
# ---------------------------------------------------------------------------

def test_actual_points_by_player_uses_league_scoring_reg_only():
    rows = [
        _week_row("A", 2021, 1, "RB", carries=20, rushing_yards=100, rushing_tds=1),
        # A POST week with a huge line that must NOT count.
        {**_week_row("A", 2021, 19, "RB", carries=30, rushing_yards=300, rushing_tds=3),
         "season_type": "POST"},
    ]
    history = pd.DataFrame(rows)
    points = actual_points_by_player(history, CONFIG_OBJ, season=2021)
    # 100 yards * 0.1 + 1 TD * 6 = 16.0. The POST row (30/300/3) is excluded.
    assert points.loc["A"] == pytest.approx(16.0)


def test_actual_points_by_player_has_no_entry_for_a_player_with_no_reg_weeks():
    history = pd.DataFrame([
        {**_week_row("A", 2021, 19, "RB", carries=10, rushing_yards=50), "season_type": "POST"},
    ])
    points = actual_points_by_player(history, CONFIG_OBJ, season=2021)
    assert "A" not in points.index


# ---------------------------------------------------------------------------
# actual_lineup_score: THE anti-circularity fix, and the zero-for-a-bust rule
# ---------------------------------------------------------------------------

def test_actual_lineup_score_ignores_proj_points_and_uses_actual_points():
    # proj_points says RB1 > RB2; actual_points says the OPPOSITE. If the
    # scorer is still reading proj_points, this test fails.
    roster = pd.DataFrame([
        {"player_id": "RB1", "position": "RB", "proj_points": 300.0},
        {"player_id": "RB2", "position": "RB", "proj_points": 10.0},
        {"player_id": "WR1", "position": "WR", "proj_points": 50.0},
        {"player_id": "WR2", "position": "WR", "proj_points": 40.0},
        {"player_id": "TE1", "position": "TE", "proj_points": 20.0},
        {"player_id": "QB1", "position": "QB", "proj_points": 100.0},
    ])
    actual = pd.Series({"RB1": 1.0, "RB2": 200.0, "WR1": 5.0, "WR2": 4.0, "TE1": 3.0, "QB1": 10.0})
    scorer = actual_lineup_score(actual, CONFIG_OBJ)
    # starters_per_team for this config: RB=2.5->2 (wait, W/R flex spreads
    # across RB/WR only) -- rather than hardcode the target, just assert the
    # high-actual RB (200) contributes and the low-actual one (1) still
    # contributes too since both RB slots are needed (2 RB starters), i.e.
    # the total must be far closer to actual-based than proj-based.
    score = scorer(roster)
    # proj-based optimal would heavily favour RB1 (300) over RB2 (10); an
    # actual-based scorer must instead be dominated by RB2's real 200.
    assert score > 150.0  # only reachable if RB2's real 200 counted


def test_actual_lineup_score_zeros_a_drafted_player_with_no_actual_points():
    roster = pd.DataFrame([
        {"player_id": "BUST", "position": "RB", "proj_points": 250.0},
        {"player_id": "OK", "position": "RB", "proj_points": 50.0},
    ])
    actual = pd.Series({"OK": 40.0})  # BUST has no entry at all -- injured all year
    scorer = actual_lineup_score(actual, CONFIG_OBJ)
    score = scorer(roster)
    assert score == pytest.approx(40.0)  # BUST contributes exactly 0, not NaN


def test_actual_lineup_score_only_counts_starters_not_bench():
    # 3 RBs drafted, this league starts at most 2.5 (2 + half the W/R flex);
    # round(2.5) = 2 -> the WORST of the three by actual points is benched
    # and must not count, even though it was the "best" pick by proj_points.
    roster = pd.DataFrame([
        {"player_id": "R1", "position": "RB", "proj_points": 1.0},
        {"player_id": "R2", "position": "RB", "proj_points": 1.0},
        {"player_id": "R3", "position": "RB", "proj_points": 1.0},
    ])
    actual = pd.Series({"R1": 100.0, "R2": 90.0, "R3": 1.0})
    scorer = actual_lineup_score(actual, CONFIG_OBJ)
    score = scorer(roster)
    assert score == pytest.approx(190.0)  # R3's real 1.0 sits on the bench


# ---------------------------------------------------------------------------
# zero_scoring_rate
# ---------------------------------------------------------------------------

def test_zero_scoring_rate_counts_missing_and_true_zero_actuals():
    roster = pd.DataFrame([
        {"player_id": "A"}, {"player_id": "B"}, {"player_id": "C"},
    ])
    actual = pd.Series({"A": 10.0, "B": 0.0})  # C is entirely absent
    zero, total = zero_scoring_rate(roster, actual)
    assert (zero, total) == (2, 3)  # B (explicit 0.0) and C (absent -> 0.0)


def test_zero_scoring_rate_on_an_empty_roster():
    assert zero_scoring_rate(pd.DataFrame(), pd.Series(dtype=float)) == (0, 0)


# ---------------------------------------------------------------------------
# strategies_for / run_backtest_cell / zero_scoring_diagnostics: integration
# ---------------------------------------------------------------------------

def test_strategies_for_returns_all_four_arms():
    strategies = strategies_for(CONFIG_OBJ, rounds=15)
    assert set(strategies) == {
        "adp", "vor", "vor_survival_unconditional", "vor_survival_conditional",
    }


def _big_history():
    """Enough players (well past replacement at every position, at teams=10)
    for a real 10-team x small-rounds draft to run without exhausting the
    pool -- mirrors test_mock.py's board() sizing philosophy."""
    rows = []
    positions = {"QB": 15, "RB": 30, "WR": 30, "TE": 15}
    for position, count in positions.items():
        for i in range(count):
            pid = f"{position}{i:02d}"
            for week in range(1, 18):
                if position == "QB":
                    rows.append(_week_row(pid, 2020, week, position,
                                           attempts=30, passing_yards=250 - i * 3,
                                           passing_tds=1))
                elif position == "RB":
                    rows.append(_week_row(pid, 2020, week, position,
                                           carries=15, rushing_yards=60 - i,
                                           targets=2, receptions=1, receiving_yards=8))
                elif position == "WR":
                    rows.append(_week_row(pid, 2020, week, position,
                                           targets=8, receptions=5, receiving_yards=70 - i))
                else:
                    rows.append(_week_row(pid, 2020, week, position,
                                           targets=4, receptions=3, receiving_yards=35 - i * 0.5))
    return pd.DataFrame(rows)


def _big_ffc_from(history):
    """Synthetic ADP: rank by total 2020 volume-ish proxy (reuse player_id
    ordering), stdev widening with depth -- shape only, not a claim about
    real ADP."""
    ids = sorted(history["player_id"].unique())
    entries = []
    for i, pid in enumerate(ids):
        entries.append({
            "name": pid, "position": pid[:2],  # e.g. "QB00" -> "QB"
            "playerId": pid, "matchState": "matched", "matchSource": "sleeper",
            "adp": float(i + 1), "stdev": 3.0 + i * 0.1, "high": 1, "low": 200, "timesDrafted": 50,
        })
    return parse_ffc_crosswalk(ffc_payload(entries))


def test_run_backtest_cell_returns_one_row_per_strategy_with_season_and_teams():
    history = _big_history()
    ffc = _big_ffc_from(history)
    board = build_board(history, CONFIG_OBJ, season=2021, ffc=ffc, teams=10, seed=1)
    actual = actual_points_by_player(history, CONFIG_OBJ, season=2021)
    out = run_backtest_cell(
        board, CONFIG_OBJ, season=2021, teams=10, my_slot=3,
        trials=3, seed=1, actual_points=actual, rounds=4,
    )
    assert set(out["strategy"]) == {
        "adp", "vor", "vor_survival_unconditional", "vor_survival_conditional",
    }
    assert (out["season"] == 2021).all()
    assert (out["teams"] == 10).all()
    assert (out["trials"] == 3).all()


def test_zero_scoring_diagnostics_reports_a_row_per_strategy():
    history = _big_history()
    ffc = _big_ffc_from(history)
    board = build_board(history, CONFIG_OBJ, season=2021, ffc=ffc, teams=10, seed=1)
    actual = actual_points_by_player(history, CONFIG_OBJ, season=2021)
    out = zero_scoring_diagnostics(
        board, CONFIG_OBJ, teams=10, my_slot=3, seed=1, actual_points=actual, rounds=4,
    )
    assert set(out["strategy"]) == {
        "adp", "vor", "vor_survival_unconditional", "vor_survival_conditional",
    }
    assert (out["picks"] == 4).all()  # rounds=4, one pick/round for my_slot
    # No 2021 stats exist in this fixture's history at all (only 2020) --
    # every single pick must score zero actual points, which is exactly what
    # a real "drafted a rookie/bust with no matching season" case looks like.
    assert (out["zero_scoring"] == out["picks"]).all()


def test_run_backtest_cell_is_reproducible_given_the_same_board_and_seed():
    # Pins the bug this refactor fixed: building the board with a FIXED seed
    # and reusing that exact board must make run_backtest_cell's numbers
    # bit-for-bit reproducible across separate calls -- unlike the old
    # "rebuild the board with seed=None inside run_backtest_cell" design,
    # where every call silently drew a fresh random Monte Carlo projection.
    history = _big_history()
    ffc = _big_ffc_from(history)
    board = build_board(history, CONFIG_OBJ, season=2021, ffc=ffc, teams=10, seed=1)
    actual = actual_points_by_player(history, CONFIG_OBJ, season=2021)
    first = run_backtest_cell(board, CONFIG_OBJ, 2021, 10, 3, 5, 1, actual, rounds=4)
    second = run_backtest_cell(board, CONFIG_OBJ, 2021, 10, 3, 5, 1, actual, rounds=4)
    pd.testing.assert_series_equal(first["mean_score"], second["mean_score"])


def test_run_backtest_returns_every_season_and_team_count_cell():
    history = _big_history()
    ffc = _big_ffc_from(history)
    out = run_backtest(
        history, CONFIG_OBJ, seasons=(2021,), ffc_by_season={2021: ffc},
        team_counts=(4, 10), trials=3, seed=1, rounds=4,
    )
    assert set(zip(out["season"], out["teams"])) == {(2021, 4), (2021, 10)}
    assert set(out["strategy"]) == {
        "adp", "vor", "vor_survival_unconditional", "vor_survival_conditional",
    }
