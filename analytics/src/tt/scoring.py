"""Half-PPR scoring.

nflverse ships `fantasy_points` (standard) and `fantasy_points_ppr` (full PPR).
Neither is this league's scoring, so points are composed from components. That
constraint happens to agree with the modelling architecture: components are what
we predict, so points are always a derived quantity anyway.
"""
from collections.abc import Mapping
from types import MappingProxyType

import pandas as pd

# Verified against the committed league-settings capture: RECEPTION modifier 0.5.
_HALF_PPR_WEIGHTS: dict[str, float] = {
    "receptions": 0.5,
    "receiving_yards": 0.1,
    "receiving_tds": 6.0,
    "carries": 0.0,          # opportunities are not scored, only their results
    "rushing_yards": 0.1,
    "rushing_tds": 6.0,
    "passing_yards": 0.04,
    "passing_tds": 4.0,
    "passing_interceptions": -1.0,
    "rushing_fumbles_lost": -2.0,
    "receiving_fumbles_lost": -2.0,
    "sack_fumbles_lost": -2.0,
}
HALF_PPR: Mapping[str, float] = MappingProxyType(_HALF_PPR_WEIGHTS)


def _is_missing(value) -> bool:
    """None, NaN and pd.NA all mean 'did not happen' and score as zero.

    Guarded because pd.isna returns an ARRAY for array-likes, which would blow
    up in a boolean context; only scalars reach the weights dict.
    """
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def score_row(row: Mapping, weights: Mapping[str, float] = HALF_PPR) -> float:
    """Points for one player-week. Absent or null stats count as zero."""
    total = 0.0
    for stat, weight in weights.items():
        value = row.get(stat)
        if _is_missing(value):
            continue
        total += float(value) * weight
    return round(total, 2)


def score_frame(df: pd.DataFrame, weights: Mapping[str, float] = HALF_PPR) -> pd.Series:
    """Vectorised `score_row` over a frame. Columns not in `weights` are ignored."""
    total = pd.Series(0.0, index=df.index)
    for stat, weight in weights.items():
        if stat in df.columns:
            total = total + df[stat].fillna(0).astype(float) * weight
    return total.round(2)


def missing_scored_columns(df: pd.DataFrame, weights: Mapping[str, float] = HALF_PPR) -> list[str]:
    """Weight keys absent from the frame.

    score_frame deliberately ignores unknown columns so it can run against
    nflverse's 150-column frames. That same tolerance hides a renamed column we
    actually need, so callers loading real data can assert on this instead.
    """
    return sorted(k for k in weights if k not in df.columns)
