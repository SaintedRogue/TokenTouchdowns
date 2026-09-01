"""Phase 2 CLI adapter (docs/draft-engine-design.md section 4): `tt.cli`.

`tt.cli` is a THIN argparse adapter over the existing analytics modules --
no analytics logic lives here (see the module's own docstring). These tests
therefore pin the WIRING: does each subcommand read the right inputs (league
config + nflverse parquet from disk, roster JSON from stdin), call the right
existing module, and write a single, valid JSON document to stdout -- never
a stray warning, a stack trace, or more than one JSON value.

Every fixture below is deliberately tiny (a handful of players, a couple of
seasons, `mc_n=30` Monte Carlo draws) so this whole file runs in well under
a second; the underlying analytics math is already pinned by
test_projections.py/test_vor.py/test_survival.py/test_draft.py/test_mock.py/
test_lineup.py/test_playoff.py and is not re-tested here.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pandas as pd
import pytest

from tt import cli
from tt.league import load_config_from_dict

CONFIG = {
    "leagueKey": "470.l.1433971", "name": "Definitely Not Bots",
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


def _write_league(tmp_path: Path, config: dict = CONFIG) -> Path:
    path = tmp_path / "league.json"
    path.write_text(json.dumps(config))
    return path


def _history_rows() -> list[dict]:
    """A handful of RB/WR/QB/TE players, two seasons, stable usage -- enough
    for `project_players` to produce a non-degenerate board (every position
    this league starts has at least one candidate)."""
    rows = []
    players = [
        ("rb1", "RB", dict(carries=18, targets=3, receptions=2, rushing_yards=80,
                            receiving_yards=15, rushing_tds=0.5, receiving_tds=0.0)),
        ("rb2", "RB", dict(carries=10, targets=1, receptions=1, rushing_yards=35,
                            receiving_yards=5, rushing_tds=0.1, receiving_tds=0.0)),
        ("wr1", "WR", dict(carries=0, targets=9, receptions=6, rushing_yards=0,
                            receiving_yards=75, rushing_tds=0.0, receiving_tds=0.5)),
        ("wr2", "WR", dict(carries=0, targets=5, receptions=3, rushing_yards=0,
                            receiving_yards=35, rushing_tds=0.0, receiving_tds=0.1)),
        ("qb1", "QB", dict(carries=3, targets=0, receptions=0, rushing_yards=12,
                            receiving_yards=0, rushing_tds=0.1, receiving_tds=0.0,
                            attempts=32, passing_yards=260, passing_tds=1.8,
                            passing_interceptions=0.5)),
        ("te1", "TE", dict(carries=0, targets=4, receptions=3, rushing_yards=0,
                            receiving_yards=30, rushing_tds=0.0, receiving_tds=0.2)),
    ]
    for season in (2024, 2025):
        for week in range(1, 18):
            for player_id, position, stats in players:
                rows.append({"player_id": player_id, "season": season, "week": week,
                             "position": position, "player_display_name": player_id.upper(),
                             **stats})
    return rows


def _write_parquet(tmp_path: Path, seasons=(2024, 2025)) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    df = pd.DataFrame(_history_rows())
    for season in seasons:
        df[df["season"] == season].to_parquet(data_dir / f"stats_player_week_{season}.parquet")
    return data_dir


def _write_adp(data_dir: Path, season: int = 2025) -> Path:
    payload = {
        "season": season,
        "meta": {"type": "Half-PPR", "teams": 12},
        "players": [
            {"name": "RB1", "position": "RB", "team": "AA", "adp": 3.0, "stdev": 1.0,
             "playerId": "rb1"},
            {"name": "WR1", "position": "WR", "team": "BB", "adp": 8.0, "stdev": 2.0,
             "playerId": "wr1"},
        ],
    }
    path = data_dir / f"ffc_adp_{season}.json"
    path.write_text(json.dumps(payload))
    return path


def _run(argv: list[str], stdin_obj=None) -> tuple[int, str, str]:
    """Invoke `cli.main` hermetically: no real stdin/stdout, no subprocess."""
    stdin = io.StringIO(json.dumps(stdin_obj) if stdin_obj is not None else "{}")
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = cli.main(argv, stdin=stdin, stdout=stdout, stderr=stderr)
    return code, stdout.getvalue(), stderr.getvalue()


# ---------------------------------------------------------------------------
# board
# ---------------------------------------------------------------------------

def test_board_returns_ranked_players_with_vor_and_tier(tmp_path):
    league = _write_league(tmp_path)
    data_dir = _write_parquet(tmp_path)
    _write_adp(data_dir)
    code, out, err = _run([
        "board", f"--config={league}", f"--data-dir={data_dir}",
        "--mc-n=30", "--seed=1",
    ])
    assert code == 0, err
    payload = json.loads(out)
    assert payload["teams"] == 4
    players = payload["players"]
    assert len(players) > 0
    assert {"player_id", "position", "proj_points", "vor", "tier", "adp"} <= players[0].keys()
    # Sorted best-first on vor.
    vors = [p["vor"] for p in players if p["vor"] is not None]
    assert vors == sorted(vors, reverse=True)


def test_board_adp_is_attached_for_players_present_in_the_crosswalk(tmp_path):
    league = _write_league(tmp_path)
    data_dir = _write_parquet(tmp_path)
    _write_adp(data_dir)
    code, out, err = _run([
        "board", f"--config={league}", f"--data-dir={data_dir}", "--mc-n=30", "--seed=1",
    ])
    assert code == 0, err
    players = {p["player_id"]: p for p in json.loads(out)["players"]}
    assert players["rb1"]["adp"] == 3.0
    # A real player absent from the (tiny, 2-player) ADP fixture stays NaN
    # (serialised as JSON null), never a fabricated number.
    assert players["rb2"]["adp"] is None


def test_board_with_slot_adds_survival_columns(tmp_path):
    league = _write_league(tmp_path)
    data_dir = _write_parquet(tmp_path)
    _write_adp(data_dir)
    code, out, err = _run([
        "board", f"--config={league}", f"--data-dir={data_dir}",
        "--mc-n=30", "--seed=1", "--slot=2",
    ])
    assert code == 0, err
    payload = json.loads(out)
    assert payload["slot"] == 2
    players = {p["player_id"]: p for p in payload["players"]}
    assert "p_gone_by_next" in players["rb1"]
    # rb1's early ADP (3.0) means near-certain to be gone by a much later pick.
    assert players["rb1"]["p_gone_by_next"] > 0.5


def test_board_without_slot_has_no_survival_columns(tmp_path):
    league = _write_league(tmp_path)
    data_dir = _write_parquet(tmp_path)
    _write_adp(data_dir)
    code, out, err = _run([
        "board", f"--config={league}", f"--data-dir={data_dir}", "--mc-n=30", "--seed=1",
    ])
    assert code == 0, err
    players = json.loads(out)["players"]
    assert "p_gone_by_next" not in players[0]


def test_board_missing_league_config_fails_with_a_helpful_message(tmp_path):
    data_dir = _write_parquet(tmp_path)
    missing = tmp_path / "nope.json"
    code, out, err = _run(["board", f"--config={missing}", f"--data-dir={data_dir}"])
    assert code != 0
    assert out == ""
    assert "tt league export" in err
    assert str(missing) in err


def test_board_missing_parquet_data_fails_with_a_helpful_message(tmp_path):
    league = _write_league(tmp_path)
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    code, out, err = _run(["board", f"--config={league}", f"--data-dir={empty_dir}"])
    assert code != 0
    assert out == ""
    assert "parquet" in err.lower()


def test_board_output_is_the_only_thing_on_stdout_even_with_an_unmodellable_scoring_warning(tmp_path):
    # This league scores Ret TD (statId 15) and 2-PT (statId 16), which
    # projections.py cannot simulate -- it emits a UserWarning (see that
    # module's `_warn_about_unmodellable_scoring`). That warning must land on
    # stderr, never corrupt the single JSON document on stdout.
    warning_config = {
        **CONFIG,
        "scoring": [
            *CONFIG["scoring"],
            {"statId": 15, "name": "Ret TD", "group": "return", "value": 6},
            {"statId": 16, "name": "2-PT", "group": "misc", "value": 2},
        ],
    }
    league = _write_league(tmp_path, warning_config)
    data_dir = _write_parquet(tmp_path)
    code, out, err = _run(["board", f"--config={league}", f"--data-dir={data_dir}", "--mc-n=30", "--seed=1"])
    assert code == 0, err
    # Exactly one line, and it parses as JSON -- a stray warning line would
    # break both of these.
    assert out.count("\n") == 1
    json.loads(out)


# ---------------------------------------------------------------------------
# pick
# ---------------------------------------------------------------------------

def test_pick_recommends_from_the_board_respecting_the_roster(tmp_path):
    league = _write_league(tmp_path)
    data_dir = _write_parquet(tmp_path)
    _write_adp(data_dir)
    code, out, err = _run([
        "pick", f"--config={league}", f"--data-dir={data_dir}",
        "--mc-n=30", "--seed=1", "--teams=4", "--slot=2", "--round=1", "--rounds=15",
    ], stdin_obj={"roster": []})
    assert code == 0, err
    payload = json.loads(out)
    assert payload["pick"] and payload["next_pick"] > payload["pick"]
    recs = payload["recommendations"]
    assert len(recs) > 0
    assert "expected_loss" in recs[0]


def test_pick_never_recommends_a_player_already_on_the_roster(tmp_path):
    league = _write_league(tmp_path)
    data_dir = _write_parquet(tmp_path)
    _write_adp(data_dir)
    code, out, err = _run([
        "pick", f"--config={league}", f"--data-dir={data_dir}",
        "--mc-n=30", "--seed=1", "--teams=4", "--slot=2", "--round=1",
    ], stdin_obj={"roster": [{"player_id": "rb1", "position": "RB"}]})
    assert code == 0, err
    ids = {r["player_id"] for r in json.loads(out)["recommendations"]}
    assert "rb1" not in ids


def test_pick_requires_slot(tmp_path):
    league = _write_league(tmp_path)
    data_dir = _write_parquet(tmp_path)
    code, out, err = _run([
        "pick", f"--config={league}", f"--data-dir={data_dir}", "--mc-n=30",
    ], stdin_obj={"roster": []})
    assert code != 0
    assert "--slot" in err


# ---------------------------------------------------------------------------
# mock
# ---------------------------------------------------------------------------

def test_mock_compares_strategies_and_reports_uncertainty(tmp_path):
    league = _write_league(tmp_path)
    data_dir = _write_parquet(tmp_path)
    _write_adp(data_dir)
    code, out, err = _run([
        "mock", f"--config={league}", f"--data-dir={data_dir}",
        "--mc-n=30", "--seed=1", "--trials=3", "--teams=4", "--rounds=1",
    ])
    assert code == 0, err
    payload = json.loads(out)
    rows = payload["strategies"]
    names = {r["strategy"] for r in rows}
    assert {"adp", "vor", "vor_survival"} <= names
    for row in rows:
        assert row["trials"] == 3
        assert "mean_score" in row and "ci95_low" in row and "ci95_high" in row


def test_mock_strategy_filter_selects_a_subset(tmp_path):
    league = _write_league(tmp_path)
    data_dir = _write_parquet(tmp_path)
    _write_adp(data_dir)
    code, out, err = _run([
        "mock", f"--config={league}", f"--data-dir={data_dir}",
        "--mc-n=30", "--seed=1", "--trials=2", "--teams=4", "--rounds=1",
        "--strategy=adp",
    ])
    assert code == 0, err
    rows = json.loads(out)["strategies"]
    assert {r["strategy"] for r in rows} == {"adp"}


def test_mock_rejects_an_unknown_strategy_name(tmp_path):
    league = _write_league(tmp_path)
    data_dir = _write_parquet(tmp_path)
    code, out, err = _run([
        "mock", f"--config={league}", f"--data-dir={data_dir}", "--strategy=made_up",
    ])
    assert code != 0
    assert "made_up" in err


# ---------------------------------------------------------------------------
# lineup
# ---------------------------------------------------------------------------

def test_lineup_optimises_a_resolved_roster(tmp_path):
    league = _write_league(tmp_path)
    data_dir = _write_parquet(tmp_path)
    roster = [
        {"player_id": "rb1", "name": "RB One", "position": "RB"},
        {"player_id": "rb2", "name": "RB Two", "position": "RB"},
        {"player_id": "wr1", "name": "WR One", "position": "WR"},
        {"player_id": "wr2", "name": "WR Two", "position": "WR"},
        {"player_id": "qb1", "name": "QB One", "position": "QB"},
        {"player_id": "te1", "name": "TE One", "position": "TE"},
    ]
    code, out, err = _run([
        "lineup", f"--config={league}", f"--data-dir={data_dir}",
        "--mc-n=30", "--seed=1", "--week=1",
    ], stdin_obj={"roster": roster})
    assert code == 0, err
    payload = json.loads(out)
    assert payload["week"] == 1
    starters = [r for r in payload["lineup"] if r["starter"]]
    assert len(starters) == sum(CONFIG["rosterSlots"].values())
    # K/DEF are not projected by this engine -- their slots come back
    # explicitly empty, never silently dropped.
    empties = {r["slot"] for r in starters if r["empty"]}
    assert "K" in empties and "DEF" in empties


def test_lineup_reports_unprojected_players_instead_of_dropping_them(tmp_path):
    league = _write_league(tmp_path)
    data_dir = _write_parquet(tmp_path)
    roster = [
        {"player_id": "rb1", "name": "RB One", "position": "RB"},
        {"player_id": None, "name": "Mystery Player", "position": "WR"},
    ]
    code, out, err = _run([
        "lineup", f"--config={league}", f"--data-dir={data_dir}", "--mc-n=30", "--seed=1",
    ], stdin_obj={"roster": roster})
    assert code == 0, err
    payload = json.loads(out)
    assert "Mystery Player" in payload["unprojected_players"]
    names = [r["name"] for r in payload["lineup"]]
    assert "Mystery Player" in names  # never silently dropped


def test_lineup_requires_a_non_empty_roster(tmp_path):
    league = _write_league(tmp_path)
    data_dir = _write_parquet(tmp_path)
    code, out, err = _run([
        "lineup", f"--config={league}", f"--data-dir={data_dir}",
    ], stdin_obj={"roster": []})
    assert code != 0
    assert "roster" in err.lower()


# ---------------------------------------------------------------------------
# playoff
# ---------------------------------------------------------------------------

def test_playoff_returns_win_probability_and_a_lineup(tmp_path):
    league = _write_league(tmp_path)
    data_dir = _write_parquet(tmp_path)
    roster = [
        {"player_id": "rb1", "name": "RB One", "position": "RB"},
        {"player_id": "rb2", "name": "RB Two", "position": "RB"},
        {"player_id": "wr1", "name": "WR One", "position": "WR"},
        {"player_id": "wr2", "name": "WR Two", "position": "WR"},
        {"player_id": "qb1", "name": "QB One", "position": "QB"},
        {"player_id": "te1", "name": "TE One", "position": "TE"},
    ]
    opponent = [dict(p, player_id=p["player_id"] + "_opp") for p in roster]
    # Opponent needs its own projectable rows too -- reuse the same
    # player_ids so both sides draw from the same tiny fixture board; give
    # the opponent literally the SAME six players (a fine stand-in here,
    # since this test only checks the response shape, not who should win).
    opponent = [dict(p) for p in roster]
    code, out, err = _run([
        "playoff", f"--config={league}", f"--data-dir={data_dir}",
        "--mc-n=30", "--seed=1", "--sim-n=200", "--week=16",
    ], stdin_obj={"roster": roster, "opponent_roster": opponent})
    assert code == 0, err
    payload = json.loads(out)
    assert 0.0 <= payload["win_probability"] <= 1.0
    assert payload["week"] == 16
    assert len(payload["lineup"]) > 0


def test_playoff_requires_an_opponent_roster(tmp_path):
    league = _write_league(tmp_path)
    data_dir = _write_parquet(tmp_path)
    roster = [{"player_id": "rb1", "name": "RB One", "position": "RB"}]
    code, out, err = _run([
        "playoff", f"--config={league}", f"--data-dir={data_dir}",
    ], stdin_obj={"roster": roster, "opponent_roster": []})
    assert code != 0
    assert "opponent" in err.lower()


# ---------------------------------------------------------------------------
# stdin / general robustness
# ---------------------------------------------------------------------------

def test_invalid_json_on_stdin_fails_cleanly(tmp_path):
    league = _write_league(tmp_path)
    data_dir = _write_parquet(tmp_path)
    stdin = io.StringIO("{not json")
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = cli.main(
        ["lineup", f"--config={league}", f"--data-dir={data_dir}"],
        stdin=stdin, stdout=stdout, stderr=stderr,
    )
    assert code != 0
    assert stdout.getvalue() == ""
    assert "json" in stderr.getvalue().lower()


def test_unknown_command_fails_cleanly():
    code, out, err = _run(["bogus"])
    assert code != 0
    assert out == ""
