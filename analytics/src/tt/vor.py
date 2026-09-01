"""Value over replacement (VOR) and tiering.

Raw projected points rank quarterbacks at the top of almost any board: even
a replacement-level QB scores heavily, because the position touches the
ball on every offensive snap, so the whole QB scoring curve sits well above
every other position's -- top to bottom, not just at the top. But a roster
only ever starts one or two QBs against two-plus RBs and WRs, so what a
player is actually WORTH to draft is never "how many points does this
player score" -- it's "how many more points does this player score than
the player I could get for free off waivers at the same position, after
every other team has filled its starting lineup." That margin is VOR, and
it is the entire reason a board built on this module reorders away from
`proj_points`'s QB-heavy top ten.

Replacement level is a direct function of two things this module takes as
EXPLICIT parameters, never as a baked-in constant: the league's starting
roster shape (`league.starters_per_team`, which already spreads flex slots
across eligible positions) and how many teams are drafting. `teams` has no
default on purpose -- the real league this project targets shows 4 teams
joined against a 10-team maximum, and those two numbers produce genuinely
different boards (see `replacement_levels`'s docstring). Defaulting `teams`
to either one would make it easy to silently draft against the wrong
league's depth.

Tiering (see `_assign_tiers`) then groups players within a position at the
points where the drop in VOR to the next player is unusually large relative
to that position's own typical drop -- the "a run on this position is
coming, reach now or wait a full round" gaps a human drafter is already
scanning for, made explicit and reproducible.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .league import LeagueConfig, starters_per_team

# How large a VOR gap has to be, relative to a position's own median gap
# between adjacent (non-tied) players, before it counts as a tier break
# rather than routine spacing. 2x is a deliberate middle ground: high enough
# that ordinary numeric noise between similarly-valued adjacent players
# never triggers a break (which would produce a new "tier" for nearly every
# player, making the label meaningless), low enough that a real run-on-a-
# position cliff -- the kind that makes a drafter reach a round early --
# still clears it easily. This is NOT fit to any labelled tier data (none
# exists for this project); a heuristic multiple-of-the-typical-gap is the
# standard, openly-approximate approach real fantasy tools use for exactly
# this problem, and it is called out here rather than presented as derived.
TIER_GAP_MULTIPLIER = 2.0


def replacement_levels(config: LeagueConfig, teams: int) -> dict[str, int]:
    """Rank of the replacement player at each position this league starts.

    `teams` is mandatory -- see module docstring for why a default would be
    actively dangerous for this project.

    Rank is `round(starters_per_team[pos] * teams)`: the number of players
    at that position needed to fill every team's starting lineup for the
    WHOLE league (flex slots already spread across eligible positions by
    `league.starters_per_team`), rounded to a whole player since "the last
    startable player" has no fractional meaning. `starters_per_team` values
    are deliberately non-terminating fractions when a flex slot splits
    unevenly (e.g. 2.333... across a 3-way RB/WR/TE flex) -- rounding here,
    once, at the point where the number becomes a rank, is what turns "how
    many slots exist" into "which draft rank is the replacement player."

    Only positions `starters_per_team` returns anything for appear here --
    i.e. only positions this league actually starts. A position nflverse
    tracks but this league doesn't roster (most of its ~25: linemen,
    defensive backs, long snappers...) has no entry, which `add_vor` treats
    as "VOR is not a meaningful concept for this position" rather than
    guessing at a rank.
    """
    return {
        position: round(count * teams)
        for position, count in starters_per_team(config).items()
    }


def _replacement_points(group: pd.DataFrame, level: int) -> float:
    """Projected points of the player at replacement rank, within one
    position's players (best to worst).

    `level` can exceed how many players are actually in `group` -- a deep
    league (high `teams`) queried against a thin dataset, or a position
    nflverse barely covers. Rather than raising or inventing a rank that
    doesn't exist, this falls back to the worst player actually available:
    an honest "we don't have data past this point," which still yields a
    usable (VOR <= 0 everywhere) column instead of crashing the whole board
    over one shallow position.
    """
    ranked = group.sort_values("proj_points", ascending=False)
    index = min(level, len(ranked)) - 1
    return float(ranked.iloc[index]["proj_points"])


def _assign_tiers(vor: pd.Series) -> pd.Series:
    """Tier number (1 = best) within one position, from gaps between
    adjacent players' VOR, sorted best to worst.

    A new tier starts after any player whose drop to the next player
    exceeds `TIER_GAP_MULTIPLIER` times the position's own median gap.

    The median -- and specifically the median of only the NONZERO gaps --
    is the right "typical gap" baseline here for two reasons. First, a mean
    would be dragged upward by the one or two genuinely huge gaps this
    function exists to detect, inflating the threshold exactly where a real
    cliff exists. Second, a position's deep end is full of near-identical
    (gap ~= 0) bench/waiver players; folding those zeros into the median
    would understate the position's typical MEANINGFUL spacing and make
    ordinary noise near the top look like a cliff by comparison. Ties
    themselves (gap == 0) never start a new tier regardless of threshold --
    there is no basis to split two players projected for the same points.

    With very few players -- or only one nonzero gap in the whole group --
    "typical" is not well established; this deliberately stays conservative
    (no break) rather than over-fitting a threshold to a sample of one.
    """
    ordered = vor.sort_values(ascending=False)
    if len(ordered) <= 1:
        return pd.Series(1, index=ordered.index, dtype=int)

    gaps = -ordered.diff().to_numpy()[1:]  # drop to the next player, best to worst
    nonzero_gaps = gaps[gaps > 0]
    median_gap = float(np.median(nonzero_gaps)) if len(nonzero_gaps) else 0.0
    threshold = median_gap * TIER_GAP_MULTIPLIER if median_gap > 0 else float("inf")

    tiers = [1]
    for gap in gaps:
        tiers.append(tiers[-1] + 1 if gap > threshold else tiers[-1])
    return pd.Series(tiers, index=ordered.index, dtype=int)


def add_vor(projections: pd.DataFrame, config: LeagueConfig, teams: int) -> pd.DataFrame:
    """Add `vor` (points above the replacement-level player at that
    position) and `tier` (a positional cluster label, 1 = best) to a
    projections table.

    `vor` is exactly 0.0 at the replacement player for a position and
    negative below it, by construction (`proj_points` minus the replacement
    player's own `proj_points`).

    VOR/tier are defined ONLY for positions `replacement_levels` has a rank
    for -- i.e. positions this league actually starts. nflverse tags roughly
    two dozen positions a week (defensive backs, linemen, long snappers...);
    this league starts QB/RB/WR/TE (K and DEF are scored from data this
    pipeline never projects in the first place -- see `league` module
    docstring). Rows at an undefined position get `vor`/`tier` = NaN rather
    than being dropped: dropping would silently shrink the table for any
    caller that still wants `proj_points` for those players (a "who's on my
    bench" or "who got misclassified" view), and pandas sorts NaN last by
    default, so a plain `sort_values("vor")` board still puts them where
    they belong with no extra filtering required.
    """
    out = projections.copy()
    levels = replacement_levels(config, teams)

    vor = pd.Series(np.nan, index=out.index, dtype=float)
    tier = pd.Series(np.nan, index=out.index, dtype=float)

    for position, group in out.groupby("position"):
        level = levels.get(position)
        if not level or level < 1:
            continue  # no (meaningful) replacement level at this position
        replacement_points = _replacement_points(group, level)
        position_vor = group["proj_points"] - replacement_points
        vor.loc[group.index] = position_vor
        tier.loc[group.index] = _assign_tiers(position_vor).astype(float)

    out["vor"] = vor
    out["tier"] = tier
    return out
