"""The pick recommender: who to actually take, not who is merely best.

The naive strategy -- take the highest-VOR player left on the board -- is
wrong whenever that player would still be there at your NEXT pick anyway.
If he survives, taking him now bought nothing you couldn't have had later,
and cost you whichever other player got taken out from under you in
between. The quantity that actually matters is the value you would LOSE by
waiting: `expected_loss = vor * P(gone before my next pick)`. A superb
player certain to survive scores near zero (you can safely wait); a merely
good player certain to vanish scores high (it is now or never). This module
answers "who should I take" by ranking on that quantity, not on `vor` alone.

`recommend` deliberately CONSUMES `p_gone_by_next` as an input column on
`board` -- it never calls `survival.add_survival` or computes a survival
probability itself. There are two competing, currently-unresolved ways to
compute that probability (unconditioned vs. conditioned on "already
observed to be available at this pick" -- see `survival.add_survival`'s
`conditional` parameter), and Task 8 settles that question empirically by
feeding this recommender boards built both ways. Keeping survival OUT of
this module is what makes swapping one model for the other, in that
experiment, a one-line change at the call site instead of a change here.
A board missing `p_gone_by_next` is therefore a caller bug (forgot to run
`add_survival`), not something this module should paper over with an
implicit default -- see the explicit check in `recommend` below.

ROSTER NEED. A board that recommends a 5th RB to a team that already has 4
is not advising, it is sorting by VOR and ignoring the roster entirely.
`roster_need` compares what a team already has against
`league.starters_per_team` (the same per-position starter target, flex
slots already spread across eligible positions, that `vor.replacement_levels`
is built from) and reports how many more starters at each position the team
still needs. `recommend` then applies `FILLED_POSITION_DISCOUNT` to
`expected_loss` for any position whose need has already been met -- a
simple, explainable "half credit" rather than a hard exclusion, because a
filled starting position can still be worth a strong bench/handcuff pick
over a marginal need elsewhere; it should rank lower, not disappear. The
threshold is starters only (not bench depth) because bench allocation is a
team-by-team judgment call this project has no data to model, whereas
starters_per_team is already the exact number `vor.py` uses for replacement
level -- reusing it keeps "how many starters does this position need" one
source of truth across the whole engine.

NEED URGENCY (F11). `FILLED_POSITION_DISCOUNT` only ever pushes DOWN a
position you already have enough of; nothing pushed UP a position you still
need but haven't been drafting -- so a real mock draft could finish with a
mandatory starting slot (say, a single starting TE) still empty, because
that position's VOR/expected_loss just never happened to rank highly enough
across 15 rounds. You cannot field a lineup with a mandatory slot unfilled,
so that is not a rare edge case to shrug off, it is a modelling defect: the
recommender needs a mechanism that actively protects a still-empty
requirement, not just one that avoids piling on a met one.

`_need_urgency` supplies that mechanism as a multiplier on `expected_loss`,
keyed off `rounds_remaining` -- how many rounds (including the one about to
be picked) are left in the whole draft, supplied by the caller since nothing
in `recommend`'s other parameters says how deep the draft goes. For a
position still short by `need` starters, `slack = rounds_remaining - need`
is how many "spare" rounds remain beyond the bare minimum of drafting one at
that position every remaining round; the multiplier is `1 +
NEED_URGENCY_STRENGTH / (slack + 1)`, which is ~1.0 (no real pressure) when
slack is large -- matching the intuition that an empty TE slot with 12
rounds left is a non-issue -- and rises toward its ceiling
`1 + NEED_URGENCY_STRENGTH` as slack shrinks toward 0, where "0 spare
rounds" means missing this position now makes it mathematically impossible
to fill later. A position with no unmet need (`need == 0`) gets no urgency
boost at all (multiplier 1.0) -- that half of roster awareness is already
`FILLED_POSITION_DISCOUNT`'s job, not this mechanism's.

This reuses `roster_need` (and therefore `league.starters_per_team`)
directly rather than inventing a second notion of "how many of this
position do I need" -- the same source `vor.replacement_levels` and this
module's own `FILLED_POSITION_DISCOUNT` already read.
"""
from __future__ import annotations

from collections import Counter

import pandas as pd

from .league import LeagueConfig, starters_per_team

# Expected-loss multiplier applied to a position whose starter need is
# already met. 0.5, not 0.0: a filled starting position can still be the
# right pick (bench depth, a handcuff, a clear tier break) -- it should
# rank BEHIND a comparable unfilled need, not be excluded outright. This is
# a deliberately simple, explainable knob (same spirit as vor.py's
# TIER_GAP_MULTIPLIER), not a value fit to any labelled draft data.
FILLED_POSITION_DISCOUNT = 0.5

# Ceiling on the need-urgency multiplier (see module docstring's "NEED
# URGENCY" section) is `1 + NEED_URGENCY_STRENGTH`, reached only at zero
# slack -- the position can no longer be filled without taking it now. 10.0
# puts that ceiling at 11x, an order of magnitude: enough to reliably
# outrank an ordinary expected_loss gap once a slot is a genuine emergency,
# without being an arbitrary 100x or 1000x. Reasoned, not fit to any
# labelled draft outcome -- same spirit and same honesty flag as
# FILLED_POSITION_DISCOUNT above and vor.py's TIER_GAP_MULTIPLIER.
NEED_URGENCY_STRENGTH = 10.0


def roster_need(roster: list[dict], config: LeagueConfig) -> dict[str, int]:
    """How many more starters, per position, this roster still needs.

    `roster` is a list of dicts with at least a `"position"` key -- what
    the team has already drafted, in whatever order. The target per
    position is `round(starters_per_team(config)[position])`: the same
    per-team starter count (flex slots already spread across eligible
    positions) that `vor.replacement_levels` scales by `teams` to get a
    league-wide replacement rank; rounding here mirrors `vor.py`'s own
    `round()` on that same quantity, for the same reason -- "how many
    starters" has no fractional meaning once you're counting one team's
    roster rather than a whole league's replacement rank.

    Never negative: a team that has rostered MORE than its starter target
    at a position (a 4th RB, a handcuff) has a need of 0, not -1 or -2 --
    a negative need would invert `recommend`'s discount logic instead of
    just capping it.
    """
    targets = {position: round(count) for position, count in starters_per_team(config).items()}
    have = Counter(entry["position"] for entry in roster)
    return {
        position: max(0, target - have.get(position, 0))
        for position, target in targets.items()
    }


def _need_urgency(position: str, need: dict[str, int], rounds_remaining: int) -> float:
    """Multiplier on `expected_loss` for a still-unfilled mandatory slot --
    see module docstring's "NEED URGENCY" section for the full reasoning.

    1.0 (no pressure) for a position with no unmet need. Otherwise
    `1 + NEED_URGENCY_STRENGTH / (slack + 1)`, where `slack =
    max(rounds_remaining - need, 0)` -- the spare rounds beyond the bare
    minimum needed to fill this position at all, floored at 0 so running
    OUT of runway (rounds_remaining < need) is just as urgent as having
    exactly none left, not somehow "more than maximally" urgent.
    """
    outstanding = need.get(position, 0)
    if outstanding <= 0:
        return 1.0
    slack = max(rounds_remaining - outstanding, 0)
    return 1.0 + NEED_URGENCY_STRENGTH / (slack + 1.0)


def recommend(
    board: pd.DataFrame,
    pick: int,
    next_pick: int,
    roster: list[dict],
    config: LeagueConfig,
    teams: int,
    rounds_remaining: int,
    n: int = 5,
) -> pd.DataFrame:
    """Rank available players by expected loss from waiting, and return the
    top `n`.

    `board` must already carry `vor` (see `vor.add_vor`) and
    `p_gone_by_next` (see `survival.add_survival`) -- this function reads
    both but computes neither. A board missing `p_gone_by_next` raises
    immediately, naming `add_survival`, rather than silently defaulting a
    survival probability: see the module docstring for why survival is
    kept strictly outside this module.

    `pick` and `next_pick` are accepted (matching `add_survival`'s
    signature) but not used to compute anything here -- they exist so this
    function's call site reads the same way `add_survival`'s does, and so
    a future caller-side sanity check (e.g. "does this board's
    `p_gone_by_next` actually match this `pick`/`next_pick` pair") has
    somewhere to live without changing the signature again. `teams` is
    likewise accepted for symmetry with `vor.add_vor`/`replacement_levels`
    (whose mandatory-`teams` reasoning is documented there) but unused here
    -- `board["vor"]` already reflects that team count by the time it
    reaches this function; there is nothing left in this module that would
    need to recompute anything league-depth-dependent.

    `rounds_remaining` -- how many rounds, INCLUDING the one about to be
    picked, are left in the whole draft -- IS used, by `_need_urgency` (see
    module docstring's "NEED URGENCY" section): it has no default and no
    fallback, deliberately, the same way `vor.replacement_levels`' `teams`
    has none -- a caller that doesn't know how deep its own draft goes has
    no business getting a silently-neutered urgency signal instead of being
    told to supply one.

    Rows with no `vor` (positions this league doesn't start -- see
    `vor.add_vor`) are dropped: `NaN * p_gone_by_next` is `NaN` and cannot
    be ranked. Rows for players already on the caller's own `roster` are
    dropped too, defensively -- `add_survival`'s board is documented to
    contain only undrafted players, so this should never actually trigger
    in a correctly wired pipeline, but a recommender that COULD resurface a
    player you already have would be actively dangerous if that invariant
    ever slipped.

    `expected_loss = vor * p_gone_by_next`, multiplied by a single combined
    factor per position: `FILLED_POSITION_DISCOUNT` if `roster_need` is
    already 0 there, else `_need_urgency`'s (>= 1.0) boost for a position
    still short -- the two are mutually exclusive per position (a filled
    position never gets boosted; an unfilled one never gets discounted), so
    multiplying them together is just "apply whichever one applies." See
    module docstring for both mechanisms. The result is sorted descending on
    the (adjusted) `expected_loss` and truncated to `n` rows.
    """
    if "p_gone_by_next" not in board.columns:
        raise ValueError(
            "board is missing 'p_gone_by_next' -- run survival.add_survival("
            "board, pick, next_pick) before calling recommend(). recommend() "
            "deliberately never computes survival itself; see tt.draft's "
            "module docstring."
        )

    already_have = {entry["player_id"] for entry in roster if "player_id" in entry}
    candidates = board[~board["player_id"].isin(already_have)]
    candidates = candidates[candidates["vor"].notna()]

    need = roster_need(roster, config)
    discount = candidates["position"].map(
        lambda position: (
            _need_urgency(position, need, rounds_remaining)
            if need.get(position, 0) > 0
            else FILLED_POSITION_DISCOUNT
        )
    )

    out = candidates.copy()
    out["expected_loss"] = out["vor"] * out["p_gone_by_next"] * discount
    return out.sort_values("expected_loss", ascending=False).head(n).reset_index(drop=True)
