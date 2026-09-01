"""Defence-vs-position reliability.

Spec §2.2 measured this on 2025 alone and found it close to noise, and NEGATIVE
against RB and TE. §9.1 requires replication across seasons before the finding is
treated as settled, because it is the justification for NOT building a full
opponent-adjustment subsystem.
"""
from __future__ import annotations

import pandas as pd


def split_half_reliability(
    df: pd.DataFrame,
    positions: tuple[str, ...] = ("QB", "RB", "WR", "TE"),
    split_week: int = 9,
    value_column: str = "points",
) -> pd.DataFrame:
    """Correlate each defence's points allowed in weeks 1..split_week against
    the rest of the season. A high r means the signal persists; a low or
    negative r means published points-allowed tables mostly measure schedule.
    """
    subset = df[df["position"].isin(positions)].copy()
    subset["half"] = (subset["week"] > split_week).map({False: "h1", True: "h2"})

    halves = (
        subset.groupby(["opponent_team", "position", "half"])[value_column]
        .mean()
        .reset_index()
    )
    wide = halves.pivot_table(
        index=["opponent_team", "position"], columns="half", values=value_column
    ).reset_index()
    wide = wide.dropna(subset=["h1", "h2"])

    out = []
    for position, group in wide.groupby("position"):
        out.append({
            "position": position,
            "teams": int(len(group)),
            "r": round(float(group["h1"].corr(group["h2"])), 3),
        })
    return pd.DataFrame(out).sort_values("position").reset_index(drop=True)
