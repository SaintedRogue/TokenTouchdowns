"""Run the out-of-sample positional draft-timing study and cache to disk.

Run from `analytics/`: `.venv/bin/python scripts/run_positional.py`
Prereqs are the same as `run_backtest.py`'s (nflverse parquet history,
`league.json`, and the FFC ADP crosswalks in `analytics/data/`).

Same instrument as the Task 8 backtest, deliberately: projections fit on
seasons strictly before S, drafted on season S's own preseason ADP, graded
on ACTUAL season-S REG points with a non-appearing player scoring zero.
The only thing that changes is what the arms disagree about -- positional
TIMING rather than the board's ranking rule.

Writes, after every cell (so a crash never loses completed work):
  positional_scores.csv       one row per (season, teams, arm)
  positional_composition.csv  mean roster shape + empty-slot count per arm
  positional_waiting.csv      per-round, per-position value lost by waiting
  positional_points.csv       Q2: raw points vs VOR, per position
"""
from __future__ import annotations

import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd  # noqa: E402
from _pos_common import CACHE, DATA, SEED, load_ffc, load_history, projection_board  # noqa: E402

from tt.league import load_config  # noqa: E402
from tt.studies.draft_board import TEAM_COUNTS, actual_points_by_player  # noqa: E402
from tt.studies.positional import (  # noqa: E402
    points_by_position,
    run_positional_study,
)
from tt.vor import add_vor  # noqa: E402

SEASONS = (2023, 2024, 2025)
TRIALS = 400
ROUNDS = 15
DONE = CACHE / "positional_DONE.marker"


def main() -> None:
    warnings.filterwarnings("ignore", category=UserWarning)
    DONE.unlink(missing_ok=True)
    start = time.time()
    config = load_config(DATA / "league.json")
    history = load_history()
    print(f"[{time.time()-start:6.1f}s] history rows={len(history)}", flush=True)

    actual_by_season = {s: actual_points_by_player(history, config, s) for s in SEASONS}

    boards: dict[tuple[int, int], pd.DataFrame] = {}
    points_rows = []
    for season in SEASONS:
        ffc = load_ffc(season)
        projected = projection_board(history, config, season, ffc, seed=SEED)
        for teams in TEAM_COUNTS:
            boards[(season, teams)] = add_vor(projected, config, teams=teams)
            table = points_by_position(
                projected, config, teams=teams, actual_points=actual_by_season[season],
            )
            table.insert(0, "season", season)
            table.insert(1, "teams", teams)
            points_rows.append(table)
        print(f"[{time.time()-start:6.1f}s] boards built for {season}", flush=True)
    pd.concat(points_rows, ignore_index=True).to_csv(CACHE / "positional_points.csv", index=False)

    scores: list[pd.DataFrame] = []
    comps: list[pd.DataFrame] = []
    waits: list[pd.DataFrame] = []
    cell_start = {"t": time.time()}

    def on_cell(season, teams, cell, composition, waiting):
        scores.append(cell)
        comps.append(composition)
        # Aggregated to (round, position) means per cell -- the raw
        # per-trial frame is ~400 x 14 x 4 rows PER CELL and is only ever
        # consumed as a mean with a standard error, which is computed here
        # while the trials are still in hand.
        grouped = waiting.groupby(["season", "teams", "round", "position"])["value_lost"]
        waits.append(
            grouped.agg(mean_lost="mean", sd="std", n="size").reset_index()
        )
        print(f"[{time.time()-start:6.1f}s] season={season} teams={teams} "
              f"cell in {time.time()-cell_start['t']:.1f}s", flush=True)
        for _, row in cell.sort_values("mean_score", ascending=False).iterrows():
            print(f"    {row['strategy']:10s} mean={row['mean_score']:8.2f} "
                  f"ci95=[{row['ci95_low']:8.2f}, {row['ci95_high']:8.2f}]", flush=True)
        cell_start["t"] = time.time()
        pd.concat(scores, ignore_index=True).to_csv(CACHE / "positional_scores.csv", index=False)
        pd.concat(comps, ignore_index=True).to_csv(CACHE / "positional_composition.csv", index=False)
        pd.concat(waits, ignore_index=True).to_csv(CACHE / "positional_waiting.csv", index=False)

    run_positional_study(
        boards, config, actual_by_season, trials=TRIALS, seed=SEED,
        rounds=ROUNDS, on_cell=on_cell,
    )
    print(f"[{time.time()-start:6.1f}s] DONE", flush=True)
    DONE.write_text(f"done at {time.time()-start:.1f}s, trials={TRIALS}, seed={SEED}\n")


if __name__ == "__main__":
    main()
