"""Turn the positional study's cached CSVs into the tables docs/positional-value.md reports.

Read-only over `analytics/data/positional/`; writes nothing but stdout.
Run from `analytics/`: `.venv/bin/python scripts/analyse_positional.py`
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from _pos_common import CACHE, DATA, SEED, load_ffc, load_history, projection_board  # noqa: E402

from tt.league import load_config  # noqa: E402
from tt.studies.draft_board import actual_points_by_player  # noqa: E402
from tt.studies.positional import POSITIONS  # noqa: E402

SEASONS = (2023, 2024, 2025)
pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 60)
pd.set_option("display.max_rows", 300)


def scores_table() -> None:
    scores = pd.read_csv(CACHE / "positional_scores.csv")
    print("\n########## Q3: arms raced out-of-sample, per cell ##########")
    for teams in sorted(scores["teams"].unique()):
        for season in sorted(scores["season"].unique()):
            cell = scores[(scores.teams == teams) & (scores.season == season)]
            if cell.empty:
                continue
            base = cell[cell.strategy == "bpa"].iloc[0]
            print(f"\n--- season={season} teams={teams}  (bpa = {base.mean_score:.1f}) ---")
            rows = []
            for _, row in cell.sort_values("mean_score", ascending=False).iterrows():
                overlap = not (row.ci95_low > base.ci95_high or row.ci95_high < base.ci95_low)
                rows.append({
                    "arm": row.strategy, "mean": row.mean_score,
                    "sd": row.std_score,
                    "ci95": f"[{row.ci95_low:.0f}, {row.ci95_high:.0f}]",
                    "vs_bpa": row.mean_score - base.mean_score,
                    "sig": "" if overlap else "SIG",
                })
            print(pd.DataFrame(rows).round(1).to_string(index=False))

    print("\n--- pooled across seasons, per team count (mean of the 3 season means) ---")
    for teams in sorted(scores["teams"].unique()):
        cell = scores[scores.teams == teams]
        base = cell[cell.strategy == "bpa"].groupby("season")["mean_score"].mean()
        rows = []
        for arm, group in cell.groupby("strategy"):
            per_season = group.set_index("season")["mean_score"]
            delta = (per_season - base).dropna()
            # SEM of the arm's own mean, pooled over the 3 season cells
            # (each cell's sem_score is the within-cell trial error).
            pooled_sem = float(np.sqrt((group["sem_score"] ** 2).sum()) / len(group))
            rows.append({
                "arm": arm, "mean": per_season.mean(),
                "sem_trials": pooled_sem,
                "vs_bpa": delta.mean(),
                "worst_season": delta.min(), "best_season": delta.max(),
                "seasons_beating_bpa": int((delta > 0).sum()),
            })
        print(f"\nteams={teams}")
        print(pd.DataFrame(rows).sort_values("vs_bpa", ascending=False).round(1).to_string(index=False))


def composition_table() -> None:
    comp = pd.read_csv(CACHE / "positional_composition.csv")
    print("\n########## Roster composition diagnostic ##########")
    for teams in sorted(comp["teams"].unique()):
        block = comp[comp.teams == teams].groupby("strategy")[
            [*POSITIONS, "empty_slots"]
        ].mean()
        print(f"\nteams={teams} (mean per drafted roster, averaged over seasons)")
        print(block.round(2).to_string())


def waiting_table() -> None:
    """Per-round, per-position value lost by waiting one turn.

    THE UNCERTAINTY REPORTED HERE IS BETWEEN-SEASON, NOT BETWEEN-TRIAL.
    Within one season the 400 trials share the same players and the same
    outcomes, so their standard error says only "how precisely do we know
    what this cost in 2024" -- which is not the question a drafter is
    asking. The three season means are the independent observations; their
    spread is the honest error bar, and with n=3 it is a wide one. A
    difference this table calls "clear" cleared 1.96 standard errors on
    THREE points, which is suggestive, not settled.
    """
    wait = pd.read_csv(CACHE / "positional_waiting.csv")
    print("\n########## Q3/Q4: value lost by waiting one turn ##########")
    for teams in sorted(wait["teams"].unique()):
        block = wait[wait.teams == teams]
        seasons_present = sorted(block["season"].unique())
        agg = block.groupby(["round", "position"])["mean_lost"].agg(
            mean="mean", lo="min", hi="max", n_seasons="size",
        )
        agg["sem"] = block.groupby(["round", "position"])["mean_lost"].sem()
        wide = agg["mean"].unstack("position")[list(POSITIONS)]
        print(f"\nteams={teams} -- mean actual points lost by waiting one turn "
              f"(seasons {seasons_present})")
        print(wide.round(1).to_string())

        rows = []
        for rnd in wide.index:
            order = wide.loc[rnd].sort_values(ascending=False)
            first, second = order.index[0], order.index[1]
            gap = order.iloc[0] - order.iloc[1]
            sem = float(np.hypot(agg.loc[(rnd, first), "sem"],
                                 agg.loc[(rnd, second), "sem"]))
            per_season = block[(block["round"] == rnd) & (block.position == first)]
            rows.append({
                "round": rnd, "take_now": first, "lost": order.iloc[0],
                "season_range": f"{agg.loc[(rnd, first), 'lo']:.0f}..{agg.loc[(rnd, first), 'hi']:.0f}",
                "all_seasons_positive": int((per_season["mean_lost"] > 0).all()),
                "runner_up": second, "runner_up_lost": order.iloc[1],
                "gap": gap, "gap_sem": sem,
                "clear": "yes" if gap > 1.96 * sem else "no",
            })
        print(f"\nteams={teams} -- highest value-lost-by-waiting position, per round")
        print(pd.DataFrame(rows).round(1).to_string(index=False))


def _ranked_actuals() -> pd.DataFrame:
    """Every ADP-ranked board player, 2023-2025, with the actual points they
    went on to score and their within-position preseason ADP rank."""
    warnings.filterwarnings("ignore", category=UserWarning)
    config = load_config(DATA / "league.json")
    history = load_history()
    frames = []
    for season in SEASONS:
        ffc = load_ffc(season)
        board = projection_board(history, config, season, ffc, seed=SEED)
        actual = actual_points_by_player(history, config, season)
        ranked = board[board["adp"].notna()].copy()
        ranked["actual"] = ranked["player_id"].map(actual).fillna(0.0)
        ranked = ranked.sort_values("adp")
        ranked["pos_rank"] = ranked.groupby("position").cumcount() + 1
        ranked["season"] = season
        frames.append(ranked[["season", "position", "pos_rank", "actual",
                              "proj_points", "adp", "name"]])
    return pd.concat(frames, ignore_index=True)


def cliff_table() -> None:
    """Where the board actually falls away, per position.

    Reported in BLOCKS of six positional ADP ranks rather than rank by rank:
    with three seasons there is exactly one observation per (season, rank),
    and the rank-to-rank noise (reported here as the per-rank standard
    deviation across seasons) is several times larger than any plausible
    single-rank cliff. A per-rank "cliff" table on this sample would be a
    picture of three seasons' luck. Blocks of six average six ranks x three
    seasons = 18 observations, which is where a real level change starts to
    separate from the noise.
    """
    allr = _ranked_actuals()
    print("\n########## Q4: where each position's board falls away ##########")
    print("\nPer-rank noise (SD of actual points across the 3 seasons, "
          "mean over ranks 1-24) -- the reason this is reported in blocks:")
    noise = (allr[allr.pos_rank <= 24].groupby(["position", "pos_rank"])["actual"]
             .std().groupby("position").mean())
    print(noise.round(1).to_string())

    for position in POSITIONS:
        block = allr[allr.position == position].copy()
        block["tier_block"] = ((block["pos_rank"] - 1) // 6) * 6 + 1
        agg = block[block.pos_rank <= 42].groupby("tier_block")["actual"].agg(
            mean="mean", sd="std", n="size",
        )
        agg["sem"] = agg["sd"] / np.sqrt(agg["n"])
        agg["drop_from_prev"] = agg["mean"].shift(1) - agg["mean"]
        agg["drop_sem"] = np.hypot(agg["sem"].shift(1), agg["sem"])
        agg["clear"] = np.where(
            agg["drop_from_prev"] > 1.96 * agg["drop_sem"], "yes", "no",
        )
        agg.index = [f"{i}-{i+5}" for i in agg.index]
        print(f"\n--- {position}: mean ACTUAL points by preseason ADP rank block "
              f"(2023-2025) ---")
        print(agg.round(1).to_string())

        proj = block[block.pos_rank <= 42].groupby("tier_block")["proj_points"].mean()
        proj.index = [f"{i}-{i+5}" for i in proj.index]
        print(f"  board's own projection for the same blocks: "
              + ", ".join(f"{k}={v:.0f}" for k, v in proj.items()))


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "scores"):
        scores_table()
    if which in ("all", "composition"):
        composition_table()
    if which in ("all", "waiting"):
        waiting_table()
    if which in ("all", "cliffs"):
        cliff_table()
