"""The in-season lineup optimiser: `argmax` expected points over LEGAL
lineups, FLEX resolved last (docs/draft-engine-design.md 3.6).

THIS IS THE ONE PLACE SLOT-FILLING LOGIC LIVES. `tt.mock.optimal_lineup_score`
originally had its own copy of this algorithm (written to fix a confirmed
defect: an earlier version spread the flex slot fractionally across its
eligible positions via `league.starters_per_team` and rounded each position
down independently, which rounded the flex slot's value away entirely --
see that function's own docstring, M-4 in fix-round-2-brief.md). That
algorithm was already correct; this module is a straight extraction of it
into something that can also answer "who starts, in which slot" -- not just
the point total -- so `mock.optimal_lineup_score` becomes a thin wrapper
around `lineup_points` instead of a second, divergence-prone copy.

WHY FLEX MUST BE RESOLVED LAST. A flex slot is not owned by any one
position -- it can be filled by whichever eligible position has the best
player left over. Filling it FIRST, greedily, by the single best
flex-eligible player leaguewide, can strand a FIXED slot that has no other
legal filler: e.g. a roster with exactly 2 RBs and a fixed 2-RB slot plus an
RB/WR flex. If the flex slot is awarded first to the single best
flex-eligible player and that happens to be an RB, only one RB remains for
the two mandatory RB slots -- an illegal lineup, or a silently short one.
Filling every fixed slot FIRST (top-`count` by points, at that exact
position) and only THEN handing each flex slot the best player left in the
remainder is what guarantees a fixed slot always gets first claim on the
only players that can ever fill it. This is a greedy two-pass assignment,
not a full assignment-problem optimum across every slot type simultaneously
-- it IS the optimum whenever a league has (as this one does) at most one
flex slot TYPE, because a flex-eligible player can only ever improve on
displacing the flex slot's own current occupant, never a fixed slot that
already claimed the best available player at its own position.

THIN ROSTERS (a position with too few eligible players -- byes, injuries,
an incomplete roster). DECISION: return a partial lineup with the empty
slot explicitly NAMED, rather than raising or silently returning fewer
starters. Raising would make this module unusable mid-week whenever any one
position is short, which is exactly when a caller most needs to see the
lineup. Silently returning fewer starters is the FIX-ROUND-2 M-4 DEFECT
CLASS THIS CODEBASE JUST FIXED for the flex slot specifically -- doing it
again here, for a different reason, would be the same bug wearing a
different position. So: every configured starting slot ALWAYS produces
exactly one output row per unit of `count` (`sum(config.roster_slots.values())`
rows total, always), and an unfillable slot is a row with `starter=True`,
`empty=True`, and every player column set to `None` -- visible, not absent.

NAN POINTS must never silently win a slot. A naive `sort_values(...).head(n)`
grabs the top `n` ROWS after sorting -- if a position has EXACTLY `n`
candidates and one has NaN points, `head(n)` takes it regardless of sort
order (there's nothing else to displace it with). Every player pool this
module selects from is therefore filtered to `points_column` non-null
BEFORE sorting, so a NaN-points player is never even a head(n) candidate --
it falls through to the bench, and its slot (if nothing else is left) is
named empty rather than handed to it.

DETERMINISM. Ties in points must resolve the same way on every run. All
sorts here use `kind="mergesort"` (pandas' only guaranteed-stable sort), so
tied players resolve by their ORIGINAL ROW ORDER IN `roster` -- a fixed,
reproducible tiebreak -- rather than whatever an unstable sort happens to
settle on, which real-world quicksort implementations do not guarantee
run-to-run for equal keys.
"""
from __future__ import annotations

import pandas as pd

from .league import FLEX_ELIGIBLE, LeagueConfig

# Columns always present on an output row, beyond whatever `roster` itself
# carries (player_id, name, position, proj_points, ...). `empty=True` marks
# a starting slot with NO legal filler (see module docstring, "thin
# rosters") -- kept as an explicit column, not inferred from `player_id`
# being null, so a caller never has to guess which column is the "no
# player" signal.
_OUTPUT_ADDED_COLUMNS = ("slot", "starter", "empty")


def _slot_plan(config: LeagueConfig) -> tuple[dict[str, int], list[tuple[str, tuple[str, ...], int]]]:
    """Split `config.roster_slots` into fixed position slots and flex
    slots, in the config's own iteration order (Python dicts preserve
    insertion order; `roster_slots` keys are unique, so no slot's count is
    ever silently merged with another's).
    """
    fixed_targets: dict[str, int] = {}
    flex_slots: list[tuple[str, tuple[str, ...], int]] = []
    for slot, count in config.roster_slots.items():
        eligible = FLEX_ELIGIBLE.get(slot)
        if eligible:
            flex_slots.append((slot, eligible, int(count)))
        else:
            fixed_targets[slot] = fixed_targets.get(slot, 0) + int(count)
    return fixed_targets, flex_slots


def _output_columns(roster: pd.DataFrame, points_column: str) -> list[str]:
    columns = list(roster.columns)
    if not columns:
        # A caller-supplied roster with literally no columns (e.g. a bare
        # `pd.DataFrame()`) still needs SOME schema to build empty-slot
        # rows against -- fall back to the minimum this module itself
        # requires to reason about a player at all.
        columns = ["player_id", "name", "position", points_column]
    elif points_column not in columns:
        columns = columns + [points_column]
    return columns


def _filled_row(player: pd.Series, slot: str) -> dict:
    return {**player.to_dict(), "slot": slot, "starter": True, "empty": False}


def _empty_slot_row(columns: list[str], points_column: str, slot: str) -> dict:
    row: dict = {col: None for col in columns}
    row[points_column] = 0.0
    row["slot"] = slot
    row["starter"] = True
    row["empty"] = True
    return row


def _eligible_pool(pool: pd.DataFrame, points_column: str) -> pd.DataFrame:
    """Candidates sorted best-first, NaN-points players excluded entirely
    (see module docstring, "NaN points") and ties broken by stable sort on
    original row order (see "determinism").
    """
    pool = pool[pool[points_column].notna()]
    return pool.sort_values(points_column, ascending=False, kind="mergesort")


def fill_lineup(
    roster: pd.DataFrame, config: LeagueConfig, points_column: str = "proj_points"
) -> pd.DataFrame:
    """The full lineup: every starting slot as its own row (filled or
    named-empty, see module docstring), followed by the bench -- every
    roster player not selected for a starting slot, ranked best-first.

    Fixed slots are filled first, each from the players AT THAT EXACT
    POSITION; only what remains is then available to flex slots, each
    filled from its own `FLEX_ELIGIBLE` positions. This order is the whole
    point of the module -- see "WHY FLEX MUST BE RESOLVED LAST" above.
    """
    fixed_targets, flex_slots = _slot_plan(config)
    columns = _output_columns(roster, points_column)

    rows: list[dict] = []
    used_index: set = set()

    for slot, count in fixed_targets.items():
        if count <= 0:
            continue
        pool = _eligible_pool(roster[roster["position"] == slot], points_column)
        top = pool.head(count)
        for _, player in top.iterrows():
            rows.append(_filled_row(player, slot))
        used_index.update(top.index)
        for _ in range(count - len(top)):
            rows.append(_empty_slot_row(columns, points_column, slot))

    remaining = roster.drop(index=used_index, errors="ignore")
    for slot, eligible, count in flex_slots:
        if count <= 0:
            continue
        pool = _eligible_pool(remaining[remaining["position"].isin(eligible)], points_column)
        top = pool.head(count)
        for _, player in top.iterrows():
            rows.append(_filled_row(player, slot))
        used_index.update(top.index)
        remaining = remaining.drop(index=top.index)
        for _ in range(count - len(top)):
            rows.append(_empty_slot_row(columns, points_column, slot))

    starters = pd.DataFrame(rows, columns=columns + list(_OUTPUT_ADDED_COLUMNS))

    bench = roster.drop(index=used_index, errors="ignore").copy()
    bench["slot"] = None
    bench["starter"] = False
    bench["empty"] = False
    if not bench.empty:
        bench = bench.sort_values(points_column, ascending=False, kind="mergesort", na_position="last")

    return pd.concat([starters, bench], ignore_index=True, sort=False)


def optimal_lineup(
    roster: pd.DataFrame, config: LeagueConfig, points_column: str = "proj_points"
) -> pd.DataFrame:
    """The starting lineup: which players start, and in which slot.

    Returns one row per configured starting slot (`starter=True`, `slot`
    naming the exact `config.roster_slots` key it fills -- e.g. the real
    league's flex slot is labelled `"W/R/T"`, not a generic "FLEX", so the
    label always traces straight back to the config that produced it) plus
    every bench player (`starter=False`, `slot=None`), ranked best-first.
    An unfillable slot (see module docstring, "thin rosters") is still one
    row, with `empty=True` and every player column `None` -- never simply
    missing.
    """
    return fill_lineup(roster, config, points_column=points_column)


def lineup_points(
    roster: pd.DataFrame, config: LeagueConfig, points_column: str = "proj_points"
) -> float:
    """Total expected points of `optimal_lineup`'s starting lineup.

    Named-empty slots contribute 0.0 (their `points_column` is set to
    exactly that in `fill_lineup`), so a thin roster's score reflects only
    the players actually able to start -- it is never inflated, and never
    raises, by a slot nothing could legally fill.
    """
    if roster.empty:
        return 0.0
    lineup = fill_lineup(roster, config, points_column=points_column)
    starters = lineup[lineup["starter"]]
    if starters.empty:
        return 0.0
    return float(starters[points_column].sum())
