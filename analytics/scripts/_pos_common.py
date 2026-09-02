"""Shared loader/board-cache helpers for the positional study scripts.

Not part of the tested engine -- a thin driver-side convenience so the two
positional scripts (`run_positional.py`, and the Q1/Q2 descriptive dump)
build each season's board exactly ONCE and reuse it from disk. The board
build itself is `studies.draft_board.build_projection_board`, unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

DATA = ROOT / "data"
CACHE = DATA / "positional"
CACHE.mkdir(parents=True, exist_ok=True)

SEED = 2026


def load_history(seasons=range(2015, 2026)) -> pd.DataFrame:
    return pd.concat(
        [pd.read_parquet(DATA / f"stats_player_week_{s}.parquet") for s in seasons],
        ignore_index=True,
    )


def projection_board(history, config, season, ffc, seed: int = SEED) -> pd.DataFrame:
    """`build_projection_board` with a disk cache keyed by (season, seed)."""
    from tt.studies.draft_board import build_projection_board

    path = CACHE / f"board_{season}_seed{seed}.parquet"
    if path.exists():
        return pd.read_parquet(path)
    board = build_projection_board(history, config, season, ffc, seed=seed)
    board.to_parquet(path, index=False)
    return board


def load_ffc(season):
    from tt.studies.draft_board import load_ffc_crosswalk

    return load_ffc_crosswalk(DATA / f"ffc_adp_{season}.json")
