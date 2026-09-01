"""ADP survival model: will this player still be on the board at my next pick?

The single most valuable rule in drafting is: do not take the highest-value
player, take the highest-value player you would LOSE. A player certain to
still be there next time costs nothing to defer; a player certain to be gone
must be taken now. Turning that instinct into arithmetic requires
`P(available at pick k)`, and this module supplies it.

FFC's ADP feed already gives, per player, exactly the input a survival model
needs: `adp` (mean draft position) and `stdev` (how tightly that position is
clustered across real drafts). Modelling a player's real draft slot as a
draw from `Normal(adp, stdev)` and asking for the probability that draw
exceeds pick `k` -- the survival function, `scipy.stats.norm.sf`, not the
CDF -- is the natural first choice: it is exactly what FFC's own `high`/`low`
columns describe informally (the earliest and latest a player has actually
gone), just parameterised.

Two inputs are handled OUTSIDE the normal model, both because the normal
CDF divides by `stdev` and both would otherwise produce a NaN that
propagates silently into every downstream VOR-weighted sum:

- `stdev` of 0 (or missing/NaN). FFC reports stdev=0 for rarely-drafted
  players -- too few sampled drafts for real variance, not "this player's
  draft slot is a mathematical certainty" in the usual sense. Treated as a
  STEP FUNCTION: certainly available strictly before `adp`, certainly gone
  at or after it.
- `adp` missing (NaN). A player absent from FFC's whole sample was drafted
  in zero of the tracked drafts -- nobody is taking them. The chosen
  convention is P(available) = 1.0 unconditionally: arguably the only
  defensible default (there is no distribution to sample from at all), and
  the one that cannot make a downstream recommender wrongly urge grabbing an
  unranked player "before they're gone."
"""
from __future__ import annotations

import pandas as pd
from scipy.stats import norm


def p_available(adp: float, stdev: float, pick: float) -> float:
    """P(this player is still on the board when pick `pick` arrives).

    Models the player's real draft slot as Normal(adp, stdev) and returns
    P(that draw > pick) via the survival function. See the module docstring
    for the two edge cases (missing adp, and stdev <= 0/NaN) handled before
    ever reaching `norm.sf`, both to avoid a division-by-zero NaN.
    """
    if adp is None or pd.isna(adp):
        return 1.0
    if stdev is None or pd.isna(stdev) or stdev <= 0:
        # Degenerate point-mass distribution: available strictly before
        # `adp`, gone at or after it. `pick == adp` counts as "gone" for the
        # same reason `norm.sf` returns exactly 0.5 there in the real
        # (non-degenerate) case -- the step function's crossover, not a
        # separate third state.
        return 1.0 if pick < adp else 0.0
    return float(norm.sf(pick, loc=adp, scale=stdev))


def _conditional_p_available_next(adp: float, stdev: float, pick: int, next_pick: int) -> float:
    """P(available at next_pick | available at pick), the statistically
    correct form -- see `add_survival`'s `conditional` parameter.

    Guarded against dividing by a denominator that is zero or numerically
    indistinguishable from it: an elite player (early `adp`) who has
    somehow fallen all the way to `pick` already has P(available at pick)
    ~ 0, meaning the Normal(adp, stdev) model has already been falsified
    for this specific player by the very fact he's still on the board.
    Dividing by that near-zero number would blow the ratio up to a huge or
    NaN value with no real meaning -- there is no more signal to extract
    from conditioning on an already-falsified model. The chosen fallback
    is the plain UNCONDITIONED estimate (the same number `conditional=False`
    would produce): a graceful degrade to an already-tested formula, rather
    than inventing a new constant or propagating inf/NaN into a downstream
    VOR-weighted sum.
    """
    unconditional_next = p_available(adp, stdev, next_pick)
    denominator = p_available(adp, stdev, pick)
    if denominator < 1e-9:
        return unconditional_next
    ratio = unconditional_next / denominator
    return min(max(ratio, 0.0), 1.0)


def add_survival(
    board: pd.DataFrame, pick: int, next_pick: int, conditional: bool = False,
) -> pd.DataFrame:
    """Decorate `board` with `p_available_next` and `p_gone_by_next`.

    `p_available_next` is, by default, `p_available(adp, stdev, next_pick)`
    -- the probability each player lasts from right now until the caller's
    NEXT pick -- and `p_gone_by_next` is its complement. These are exactly
    what the recommender multiplies by VOR (design doc §3.4: expected cost
    of waiting is `VOR(player) x P(gone before my next pick)`).

    Both `pick` (the pick happening right now) and `next_pick` are required,
    even though only `next_pick` feeds the unconditioned `p_available` --
    `pick` is used to reject a `next_pick` that isn't strictly after it.
    "Will this player last from my pick to my next one" is only a
    meaningful question looking forward; a caller passing `next_pick <=
    pick` has a bug, not a real probability to compute, so this fails
    loudly rather than silently returning a survival probability for the
    wrong direction.

    `conditional` (default False, so all existing callers and tests are
    unaffected): when True, `p_available_next` is instead `P(available at
    next_pick | available at pick)` = `p_available(adp, stdev, next_pick) /
    p_available(adp, stdev, pick)`, clipped to [0, 1]. The board this
    decorates only ever contains players OBSERVED to still be available at
    `pick` -- that's a fact about how the board was built, not a modelling
    assumption -- so conditioning on it is the statistically correct form;
    the unconditioned default instead treats "survived to pick" as if it
    were still unknown. Which form the recommender should actually use is
    an open empirical question (Task 8 settles it by feeding both to the
    same recommender), which is exactly why this is a caller-chosen flag
    rather than a silent change to the default.
    """
    if next_pick <= pick:
        raise ValueError(
            f"next_pick ({next_pick}) must be strictly after pick ({pick}) -- "
            "add_survival answers 'will they last from my pick now to my next "
            "one', which only makes sense looking forward."
        )
    out = board.copy()
    if conditional:
        out["p_available_next"] = [
            _conditional_p_available_next(row.adp, row.stdev, pick, next_pick)
            for row in board.itertuples()
        ]
    else:
        out["p_available_next"] = [
            p_available(row.adp, row.stdev, next_pick) for row in board.itertuples()
        ]
    out["p_gone_by_next"] = 1.0 - out["p_available_next"]
    return out
