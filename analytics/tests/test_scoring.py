import pandas as pd
from tt.scoring import HALF_PPR, score_row, score_frame, missing_scored_columns


def test_half_ppr_gives_half_a_point_per_reception():
    assert HALF_PPR["receptions"] == 0.5


def test_half_ppr_is_immutable():
    """HALF_PPR constant must not be mutated by callers."""
    try:
        HALF_PPR["test_key"] = 999
        assert False, "should have raised TypeError on mutation attempt"
    except TypeError:
        pass  # expected


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


def test_score_row_handles_float_nan():
    """NaN from numpy/pandas floating ops."""
    row = {"receiving_yards": float('nan'), "receptions": 2}
    assert score_row(row) == 1.0


def test_score_row_handles_pd_na():
    """pd.NA from nullable dtypes (pyarrow-backed columns from nflverse)."""
    row = {"receptions": pd.NA, "receiving_yards": 80}
    assert score_row(row) == 8.0


def test_score_row_and_frame_agree_on_pd_na():
    """score_row and score_frame must handle pd.NA identically."""
    df = pd.DataFrame([{"receptions": pd.NA, "receiving_yards": 100}])
    frame_score = score_frame(df).iloc[0]
    row_score = score_row(df.to_dict(orient='records')[0])
    assert frame_score == row_score == 10.0


def test_score_row_rushes_fumbles_lost():
    row = {"rushing_yards": 50, "rushing_tds": 1, "rushing_fumbles_lost": 1}
    # 5.0 + 6.0 - 2.0 = 9.0
    assert score_row(row) == 9.0


def test_score_row_receiving_fumbles_lost():
    row = {"receptions": 3, "receiving_yards": 20, "receiving_fumbles_lost": 1}
    # 1.5 + 2.0 - 2.0 = 1.5
    assert score_row(row) == 1.5


def test_score_row_sack_fumbles_lost():
    row = {"passing_yards": 400, "sack_fumbles_lost": 2}
    # 16.0 - 4.0 = 12.0
    assert score_row(row) == 12.0


def test_score_frame_rushes_fumbles_lost():
    df = pd.DataFrame([
        {"rushing_yards": 50, "rushing_tds": 1, "rushing_fumbles_lost": 1},
    ])
    assert list(score_frame(df)) == [9.0]


def test_score_frame_scores_every_row():
    df = pd.DataFrame([
        {"receptions": 6, "receiving_yards": 80, "receiving_tds": 1},
        {"carries": 20, "rushing_yards": 100, "rushing_tds": 2},
    ])
    assert list(score_frame(df)) == [17.0, 22.0]


def test_score_frame_ignores_columns_that_are_not_scored():
    df = pd.DataFrame([{"receptions": 2, "target_share": 0.9, "player_name": "X"}])
    assert list(score_frame(df)) == [1.0]


def test_missing_scored_columns_returns_empty_when_all_present():
    df = pd.DataFrame([{
        "receptions": 1,
        "receiving_yards": 10,
        "receiving_tds": 0,
        "carries": 0,
        "rushing_yards": 0,
        "rushing_tds": 0,
        "passing_yards": 0,
        "passing_tds": 0,
        "passing_interceptions": 0,
        "rushing_fumbles_lost": 0,
        "receiving_fumbles_lost": 0,
        "sack_fumbles_lost": 0,
    }])
    assert missing_scored_columns(df) == []


def test_missing_scored_columns_detects_absent_stat():
    df = pd.DataFrame([{"receptions": 1}])
    missing = missing_scored_columns(df)
    assert "receiving_yards" in missing
    assert "rushing_tds" in missing


def test_missing_scored_columns_ignores_extra_columns():
    df = pd.DataFrame([{
        "receptions": 1,
        "receiving_yards": 10,
        "receiving_tds": 0,
        "carries": 0,
        "rushing_yards": 0,
        "rushing_tds": 0,
        "passing_yards": 0,
        "passing_tds": 0,
        "passing_interceptions": 0,
        "rushing_fumbles_lost": 0,
        "receiving_fumbles_lost": 0,
        "sack_fumbles_lost": 0,
        "target_share": 0.5,
        "player_name": "X",
    }])
    assert missing_scored_columns(df) == []
