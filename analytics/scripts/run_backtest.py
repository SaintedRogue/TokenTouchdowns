"""Task 8: run the real, out-of-sample draft-strategy backtest and cache
results to disk (gitignored `analytics/data/`) so a crash doesn't lose the
run.

Run from `analytics/`: `.venv/bin/python scripts/run_backtest.py`
Prereqs (run once, or whenever the crosswalk should refresh):
  1. `.venv/bin/python scripts/export_nflverse_roster.py`
  2. `node ../analytics/scripts/build_ffc_crosswalk.mjs` (from repo root)
Both write into `analytics/data/`, which this script then reads.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pandas as pd  # noqa: E402

from tt.league import load_config  # noqa: E402
from tt.studies.draft_board import (  # noqa: E402
    BACKTEST_SEASONS,
    TEAM_COUNTS,
    load_ffc_crosswalk,
    run_backtest_cell,
    zero_scoring_diagnostics,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TRIALS = 200
SEED = 2026
ROUNDS = 15


def load_history(seasons) -> pd.DataFrame:
    frames = [pd.read_parquet(DATA_DIR / f"stats_player_week_{s}.parquet") for s in seasons]
    return pd.concat(frames, ignore_index=True)


def my_slot_for(teams: int) -> int:
    return teams // 2


def main() -> None:
    t_start = time.time()
    config = load_config(DATA_DIR / "league.json")
    # Full available history (2015..2025): build_projection_board itself
    # restricts training to seasons strictly before each backtest season, so
    # handing it everything is safe and lets it use the full 2015..S-1
    # window the brief specifies.
    history = load_history(range(2015, 2026))
    print(f"[{time.time()-t_start:6.1f}s] loaded history: {len(history)} rows", flush=True)

    ffc_by_season = {
        season: load_ffc_crosswalk(DATA_DIR / f"ffc_adp_{season}.json")
        for season in BACKTEST_SEASONS
    }
    for season, ffc in ffc_by_season.items():
        resolved = ffc["player_id"].notna().sum()
        print(f"[{time.time()-t_start:6.1f}s] {season} ffc crosswalk: "
              f"{resolved}/{len(ffc)} resolved", flush=True)

    results = []
    zero_rows = []
    for season in BACKTEST_SEASONS:
        for teams in TEAM_COUNTS:
            slot = my_slot_for(teams)
            cell_start = time.time()
            cell = run_backtest_cell(
                history, config, season, ffc_by_season[season], teams, slot,
                trials=TRIALS, seed=SEED, rounds=ROUNDS,
            )
            results.append(cell)
            print(f"[{time.time()-t_start:6.1f}s] season={season} teams={teams} "
                  f"my_slot={slot} done in {time.time()-cell_start:.1f}s", flush=True)
            for _, row in cell.iterrows():
                print(f"    {row['strategy']:28s} mean={row['mean_score']:8.2f} "
                      f"std={row['std_score']:7.2f} ci95=[{row['ci95_low']:.2f}, "
                      f"{row['ci95_high']:.2f}]", flush=True)

            zeros = zero_scoring_diagnostics(
                history, config, season, ffc_by_season[season], teams, slot,
                seed=SEED, rounds=ROUNDS,
            )
            zeros.insert(0, "season", season)
            zeros.insert(1, "teams", teams)
            zero_rows.append(zeros)

            # Cache after every cell so a crash never loses completed work.
            pd.concat(results, ignore_index=True).to_csv(DATA_DIR / "backtest_results.csv", index=False)
            pd.concat(zero_rows, ignore_index=True).to_csv(DATA_DIR / "backtest_zero_scoring.csv", index=False)

    print(f"[{time.time()-t_start:6.1f}s] DONE", flush=True)


if __name__ == "__main__":
    main()
