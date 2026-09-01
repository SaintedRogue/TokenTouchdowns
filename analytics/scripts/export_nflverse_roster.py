"""Export nflverse's own player roster for `build_ffc_crosswalk.mjs`'s
fallback match path.

Sleeper's `gsis_id` field (the primary crosswalk path -- see that script's
module docstring) has real, material gaps for exactly the players a
draft-strategy backtest cares about most: several 2021+ elite skill players
(Ja'Marr Chase, Bijan Robinson, Amon-Ra St. Brown, Puka Nacua, Garrett
Wilson, Jahmyr Gibbs -- verified live against Sleeper's API 2026-09-01)
carry `gsis_id: null` there. This script gives the Node crosswalk script a
second reference set to resolve against: nflverse's own player_id, name,
position and most-recent team, one row per player, QB/RB/WR/TE only (the
only positions this pipeline ever drafts -- see
`projections.PROJECTABLE_POSITIONS`).

Run from `analytics/`: `.venv/bin/python scripts/export_nflverse_roster.py`
Writes `analytics/data/nflverse_players.json` (gitignored).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pandas as pd  # noqa: E402

from tt.projections import PROJECTABLE_POSITIONS  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SEASONS = range(2015, 2026)


def build_roster(data_dir: Path = DATA_DIR, seasons=SEASONS) -> list[dict]:
    frames = [
        pd.read_parquet(
            data_dir / f"stats_player_week_{season}.parquet",
            columns=["player_id", "player_name", "player_display_name", "position", "team", "season"],
        )
        for season in seasons
    ]
    history = pd.concat(frames, ignore_index=True)
    history = history[history["position"].isin(PROJECTABLE_POSITIONS)]

    # Most-recent-season name/team per player, same fallback order as
    # `projections._resolve_names`: display name, then player_name, then the
    # bare player_id -- sorted by season first so "most recent" is
    # well-defined regardless of input row order.
    name = history["player_display_name"].replace("", None)
    name = name.fillna(history["player_name"].replace("", None))
    name = name.fillna(history["player_id"])
    history = history.assign(_name=name).sort_values("season")

    latest = history.groupby("player_id").last()[["_name", "position", "team"]].reset_index()
    latest.columns = ["playerId", "name", "position", "team"]
    return latest.to_dict(orient="records")


def main() -> None:
    records = build_roster()
    out_path = DATA_DIR / "nflverse_players.json"
    out_path.write_text(json.dumps(records))
    print(f"wrote {len(records)} players to {out_path}")


if __name__ == "__main__":
    main()
