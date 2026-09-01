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

    A position with fewer than two teams having both halves present yields an
    undefined correlation (r = NaN, via pandas' own Series.corr): reported
    ``teams`` is what makes that shrunken sample visible to the caller, rather
    than the NaN silently looking like a normal result.
    """
    empty_result = pd.DataFrame(columns=["position", "teams", "r"])

    # A wholly empty frame (no rows, possibly no columns at all) has no
    # "position" column to index -- guard before touching it rather than
    # letting a bare df["position"] raise KeyError.
    if df.empty or "position" not in df.columns:
        return empty_result

    subset = df[df["position"].isin(positions)].copy()
    if subset.empty:
        return empty_result

    subset["half"] = (subset["week"] > split_week).map({False: "h1", True: "h2"})

    halves = (
        subset.groupby(["opponent_team", "position", "half"])[value_column]
        .mean()
        .reset_index()
    )
    wide = halves.pivot_table(
        index=["opponent_team", "position"], columns="half", values=value_column
    ).reset_index()
    # A position present in `positions` but absent from the data (or one that
    # never has rows on both sides of split_week) produces a pivot with no
    # h1/h2 columns at all -- dropna(subset=[...]) on missing columns raises
    # KeyError rather than just yielding zero rows, so guard first.
    if "h1" not in wide.columns or "h2" not in wide.columns:
        return empty_result
    wide = wide.dropna(subset=["h1", "h2"])
    if wide.empty:
        return empty_result

    out = []
    for position, group in wide.groupby("position"):
        out.append({
            "position": position,
            "teams": int(len(group)),
            "r": round(float(group["h1"].corr(group["h2"])), 3),
        })
    return pd.DataFrame(out).sort_values("position").reset_index(drop=True)
