import pandas as pd
from tt.scoring import HALF_PPR, score_row, score_frame


def test_half_ppr_gives_half_a_point_per_reception():
    assert HALF_PPR["receptions"] == 0.5


def test_score_row_composes_receiving_line():
    # 6 catches, 80 yards, 1 TD = 3.0 + 8.0 + 6.0
    row = {"receptions": 6, "receiving_yards": 80, "receiving_tds": 1}
    assert score_row(row) == 17.0


def test_score_row_composes_rushing_line():
    # 20 carries for 100 yards and 2 TDs = 10.0 + 12.0 (carries are not scored)
    row = {"carries": 20, "rushing_yards": 100, "rushing_tds": 2}
    assert score_row(row) == 22.0


def test_score_row_applies_negative_scoring():
    row = {"passing_yards": 250, "passing_tds": 2, "passing_interceptions": 1}
    # 10.0 + 8.0 - 1.0
    assert score_row(row) == 17.0


def test_score_row_treats_missing_and_null_stats_as_zero():
    assert score_row({}) == 0.0
    assert score_row({"receiving_yards": None, "receptions": 2}) == 1.0


def test_score_frame_scores_every_row():
    df = pd.DataFrame([
        {"receptions": 6, "receiving_yards": 80, "receiving_tds": 1},
        {"carries": 20, "rushing_yards": 100, "rushing_tds": 2},
    ])
    assert list(score_frame(df)) == [17.0, 22.0]


def test_score_frame_ignores_columns_that_are_not_scored():
    df = pd.DataFrame([{"receptions": 2, "target_share": 0.9, "player_name": "X"}])
    assert list(score_frame(df)) == [1.0]
