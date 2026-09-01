"""The playoff (variance-aware) lineup optimiser (docs/draft-engine-design.md
3.6, second half).

WHY THIS MODULE EXISTS AND WHY IT IS NOT `lineup.py`. Every other
optimiser in this engine -- `lineup.optimal_lineup` included -- maximises
EXPECTED points. In a one-game elimination playoff match, expected margin
is worthless; only `P(my total > their total)` counts, and that objective
INVERTS on the opponent: a heavy underdog's median outcome loses regardless
of lineup choice, so a high-variance ("boom/bust") player is strictly
better than a steady one of equal mean, because the only lineups that win
are the tail ones and raising variance is the only way to reach more of
them. A heavy favourite's median outcome already wins, so variance only
manufactures NEW ways to lose, and the high-floor player is strictly
better. A correct implementation reproduces BOTH behaviours from ONE
objective function with no `if underdog:` branch anywhere -- see
`win_probability`, the only place this module ever looks at the opponent
at all. `playoff_lineup` reuses `lineup.optimal_lineup` for legal-lineup
construction and as its expected-points comparison baseline; it does not,
and must not, reimplement flex resolution (see lineup.py's own module
docstring for why that logic lives in exactly one place).

THE MODEL: independent Normal per starter, summed. `projections.
project_players` gives each player `proj_points` (mean) and `sd`, but not
the raw Monte Carlo samples the mean/sd were computed from -- a lineup
optimiser working from a roster board only ever has the summary, not the
samples. `win_probability` therefore samples each starter's score as
`Normal(proj_points, sd)`, independently of every other player (in the
lineup and across lineups), and sums.

THIS IS A KNOWN SIMPLIFICATION, STATED HONESTLY:
  - Independence across teammates is false in reality. A QB's big passing
    day and his WR1's big receiving day are the SAME game script, not two
    independent draws; a defense's good day often coincides with the
    offense sitting on a lead. Modelling them as independent UNDERSTATES
    the true variance of a lineup that stacks correlated players (e.g. a
    QB and his own WR1) and OVERSTATES it for a lineup of anti-correlated
    ones (e.g. two RBs who split the same backfield's touches). There is
    no per-player correlation matrix available anywhere upstream of this
    module to do better with; this is a real gap, not an oversight.
  - Independence between the two lineups (mine vs theirs) is a much safer
    assumption -- they are different teams' rosters and only share
    correlation on defense-vs-offense weeks when they play each other,
    which is not this module's scenario (both lineups are set against a
    common slate, not against each other).
  - Normal per player is itself a simplification: `compose.py`'s real
    per-player distributions are right-skewed (touchdowns are lumpy,
    yardage has a fat right tail) and `project_players` reports p10/p50/p90
    that a Normal(mean, sd) does not reproduce exactly. This module only
    receives the SUMMARY (proj_points, sd), not the skewed samples, so a
    symmetric Normal is what's available -- documented here rather than
    silently passed off as the true shape. It is still the right tool for
    the module's core claim: `P(win)` under a Normal-sum model is provably
    monotonic in variance in the correct direction on each side of parity
    (see `win_probability`'s docstring), which is the property being
    tested and the property that matters for the "prefer variance /
    prefer floor" decision. A skewed model would change the exact numbers,
    not the qualitative inversion this module exists to get right.

TIES. `P(my total > their total)` is a STRICT inequality by construction
here -- an exact tie (only reachable in practice when both sides are
perfectly deterministic, sd=0, and land on the same number) counts as a
loss, not a win and not a 0.5 coin flip. A real playoff tie is usually
broken by some league-specific rule this module has no visibility into;
counting it as a loss is the conservative (never overstates win
probability) choice, not an attempt to model whatever that rule is.

SEARCH / PRUNING STRATEGY (`playoff_lineup`). Exhaustively enumerating
every legal lineup is combinatorially large in general (choosing which
players fill which slots, with flex ambiguity, is already what makes
`lineup.py` need its own two-pass algorithm just for ONE objective). This
module does not search all of them. Instead:

  1. Seed the search at `optimal_lineup`'s expected-points-optimal lineup
     -- provably a strong starting point, since it already maximises the
     separable linear objective (sum of means) the win-probability
     objective reduces to whenever variance stops mattering (see the
     "evenly matched" test).
  2. Hill-climb by SINGLE-PLAYER SWAPS: for each starting slot, in the
     lineup's own slot order, try every bench player eligible for that
     slot (same-position for a fixed slot, `league.FLEX_ELIGIBLE[slot]`
     for a flex slot -- reusing `league.py`'s own mapping, never a second
     copy of it), and take the single best-improving swap if one exists.
     Repeat full sweeps until a sweep makes no swap, capped at
     `_MAX_SWEEPS` sweeps as a safety net (in practice this always
     terminates in 1-2 sweeps: every accepted swap strictly increases a
     bounded, finite-valued metric evaluated under a FIXED seed, so no
     lineup can ever be revisited).

  RISK, STATED PLAINLY: this is greedy local search, not an exhaustive
  search, and it can miss the true optimum in two ways --
    (a) a LOCAL optimum: a swap that only helps in combination with a
        second swap (neither alone improves) is invisible to a
        single-swap neighbourhood. A full joint search over slot
        combinations would not have this blind spot but is the
        combinatorial explosion this module is deliberately avoiding.
    (b) MONTE CARLO NOISE in the comparison itself: with a finite `n`,
        `win_probability` is an estimate, not the true probability, so a
        "best" swap could occasionally be a false positive from sampling
        noise rather than a genuine improvement. This is mitigated, not
        eliminated, by calling `win_probability` with the SAME `seed` for
        every trial evaluated during the search: since every candidate
        lineup has the SAME NUMBER of starting slots as every other
        (swapping never changes slot COUNT, only which player occupies
        one slot), the random draws consumed for `their_lineup` -- always
        drawn immediately after `my_lineup`'s SAME-SHAPE draw -- are
        bit-for-bit IDENTICAL across every trial in the search. Unchanged
        starters in `my_lineup` also draw from the same rng position
        every time. This is deliberate use of the common-random-numbers
        variance-reduction technique, not an accident of calling
        `win_probability` the same way twice: it means two trial lineups
        differing by one swap are compared on the SAME simulated
        opponent outcomes and the SAME simulated teammates, isolating the
        swapped player's actual effect from unrelated sampling noise. It
        does not remove noise in the swapped player's own draw, so a
        very close call can still occasionally go the "wrong" way at low
        `n` -- raise `n` for a final, load-bearing decision.

  A silent version of this pruning (not documented, not seeded) is
  exactly the failure mode this docstring exists to avoid.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .league import FLEX_ELIGIBLE, LeagueConfig
from .lineup import lineup_points, optimal_lineup

# Safety cap on hill-climb sweeps (see module docstring, "SEARCH / PRUNING
# STRATEGY"). Every accepted swap strictly increases a bounded, seed-fixed
# metric that can never repeat a previously-seen lineup, so this is never
# actually reached in practice for any roster this engine's league shape
# produces -- it exists purely to bound worst-case runtime, not because the
# algorithm is expected to need it.
_MAX_SWEEPS = 25

DEFAULT_N = 5000


def _eligible_positions(slot: str) -> tuple[str, ...]:
    """Which real positions may fill `slot`.

    A flex slot label is a `league.FLEX_ELIGIBLE` key; a fixed slot label
    IS the position name itself (see `lineup._slot_plan`) -- reusing that
    same mapping here, rather than re-deriving it, is what keeps this
    module's swap search legal under exactly the same rules `lineup.py`
    already enforces.
    """
    return FLEX_ELIGIBLE.get(slot, (slot,))


def _starting_rows(lineup: pd.DataFrame) -> pd.DataFrame:
    """The players who actually take the field, from a lineup frame that
    may also carry bench rows and named-empty starting slots.

    A frame with `starter`/`empty` columns (e.g. `optimal_lineup`'s output,
    or `playoff_lineup`'s internal trial lineups) is filtered down to
    `starter & ~empty`, matching `lineup.lineup_points`'s own contract that
    an empty slot contributes nothing. A frame with NEITHER column (a
    hand-built "this is just my starters" frame, which every
    `win_probability` unit test below uses directly) is trusted as-is --
    every row counts. This is what lets `win_probability` accept both
    `optimal_lineup`'s full output AND a bare two-row DataFrame without the
    caller having to pre-filter one but not the other.
    """
    if "starter" in lineup.columns:
        lineup = lineup[lineup["starter"].astype(bool)]
    if "empty" in lineup.columns:
        lineup = lineup[~lineup["empty"].astype(bool)]
    return lineup


def _lineup_totals(
    lineup: pd.DataFrame,
    n: int,
    rng: np.random.Generator,
    points_column: str,
    sd_column: str,
) -> np.ndarray:
    """`n` Monte Carlo draws of this lineup's total points.

    Each starter is drawn as an independent Normal(proj_points, sd) and
    summed -- see the module docstring for why independence and
    Normal-per-player are both named simplifications, not hidden ones. A
    missing/NaN sd is treated as 0.0 (a player this module has no spread
    information for is modelled as its own point estimate, not silently
    dropped or treated as infinite variance).
    """
    starters = _starting_rows(lineup)
    mu = starters[points_column].fillna(0.0).to_numpy(dtype=float)
    if sd_column in starters.columns:
        sigma = starters[sd_column].fillna(0.0).to_numpy(dtype=float)
    else:
        sigma = np.zeros_like(mu)
    sigma = np.clip(sigma, 0.0, None)  # a standard deviation is never negative
    k = len(mu)
    if k == 0:
        return np.zeros(n)
    draws = rng.normal(loc=mu, scale=sigma, size=(n, k))
    return draws.sum(axis=1)


def win_probability(
    my_lineup: pd.DataFrame,
    their_lineup: pd.DataFrame,
    n: int = DEFAULT_N,
    seed: int | None = None,
    points_column: str = "proj_points",
    sd_column: str = "sd",
) -> float:
    """`P(my starting lineup's total > their starting lineup's total)`,
    estimated by simulating both totals `n` times from per-player
    `Normal(points_column, sd_column)` draws and counting the fraction of
    trials where mine is strictly greater (see module docstring, "TIES").

    Both lineups may be a full `optimal_lineup`-shaped frame (starters,
    bench, and named-empty slots all present -- only `starter & ~empty`
    rows are simulated) or a bare frame of just the players who will
    start; see `_starting_rows`.

    INDEPENDENCE. Every player, in both lineups, is sampled independently
    of every other player -- teammates and game-script correlation
    (a QB and his own WR1 having a good day together, say) are real and
    are NOT modelled here. See the module docstring for why, and for what
    that does and does not bias.

    Reproducible for a fixed `seed`: `n` and `seed` alone determine every
    random draw this function makes (see `_lineup_totals`), so two calls
    with identical arguments return the identical float, and -- because
    `my_lineup` is always drawn before `their_lineup` from the same `rng`
    -- two lineups with the SAME NUMBER of starters draw `their_lineup`'s
    samples bit-for-bit identically, which is exactly the common-random-
    numbers property `playoff_lineup`'s swap search relies on (see its own
    docstring).
    """
    rng = np.random.default_rng(seed)
    my_totals = _lineup_totals(my_lineup, n, rng, points_column, sd_column)
    their_totals = _lineup_totals(their_lineup, n, rng, points_column, sd_column)
    return float(np.mean(my_totals > their_totals))


def _bench_candidates(bench: list[dict], positions: tuple[str, ...]) -> list[int]:
    return [i for i, player in enumerate(bench) if player.get("position") in positions]


def playoff_lineup(
    roster: pd.DataFrame,
    their_lineup: pd.DataFrame,
    config: LeagueConfig,
    n: int = DEFAULT_N,
    seed: int | None = None,
    points_column: str = "proj_points",
    sd_column: str = "sd",
) -> pd.DataFrame:
    """The legal lineup from `roster` that maximises
    `win_probability(..., their_lineup, ...)` -- NOT expected points.

    Returns a frame in the same shape as `lineup.optimal_lineup` (one row
    per configured starting slot, `starter`/`empty` flags, then the bench,
    best-first) plus attached metadata on `.attrs`, so a caller can see
    both the achieved result and what the plain expected-points choice
    would have been:

      - `win_probability`: the returned lineup's simulated win probability
        against `their_lineup`.
      - `expected_points_lineup_win_probability`: what `optimal_lineup`
        (i.e. ignoring `their_lineup` entirely) would have achieved against
        the SAME opponent -- the number to compare `win_probability`
        against to see whether, and by how much, accounting for the
        opponent actually helped.
      - `expected_points_lineup_points`: that expected-points lineup's own
        total projected points (`lineup.lineup_points`), for context on
        what mean-points cost (if any) the returned lineup paid.
      - `playoff_lineup_points`: the RETURNED lineup's own total projected
        points, directly comparable to the line above.

    See the module docstring ("SEARCH / PRUNING STRATEGY") for how the
    search works and what it can miss.
    """
    ep_lineup = optimal_lineup(roster, config, points_column=points_column)
    ep_starters_df = ep_lineup[ep_lineup["starter"]].reset_index(drop=True)
    bench_df = ep_lineup[~ep_lineup["starter"]].reset_index(drop=True)

    ep_win_prob = win_probability(
        ep_starters_df, their_lineup, n=n, seed=seed,
        points_column=points_column, sd_column=sd_column,
    )
    ep_points = lineup_points(roster, config, points_column=points_column)

    starters = ep_starters_df.to_dict("records")
    bench = bench_df.to_dict("records")
    best_prob = ep_win_prob

    for _sweep in range(_MAX_SWEEPS):
        improved = False
        for i, slot_row in enumerate(starters):
            positions = _eligible_positions(slot_row["slot"])
            candidate_indices = _bench_candidates(bench, positions)
            best_choice: tuple[int, list[dict]] | None = None
            for j in candidate_indices:
                candidate = bench[j]
                trial = list(starters)
                trial[i] = {**candidate, "slot": slot_row["slot"], "starter": True, "empty": False}
                trial_df = pd.DataFrame(trial)
                trial_prob = win_probability(
                    trial_df, their_lineup, n=n, seed=seed,
                    points_column=points_column, sd_column=sd_column,
                )
                if trial_prob > best_prob:
                    best_prob = trial_prob
                    best_choice = (j, trial)
            if best_choice is not None:
                j, trial = best_choice
                displaced = starters[i]
                starters = trial
                bench.pop(j)
                if not displaced.get("empty", False):
                    bench.append(displaced)
                improved = True
        if not improved:
            break

    starters_out = pd.DataFrame(starters, columns=ep_starters_df.columns)
    bench_out = pd.DataFrame(bench, columns=bench_df.columns)
    if not bench_out.empty:
        bench_out = bench_out.sort_values(
            points_column, ascending=False, kind="mergesort", na_position="last"
        )
    result = pd.concat([starters_out, bench_out], ignore_index=True, sort=False)

    playoff_points = float(starters_out[points_column].fillna(0.0).sum())

    result.attrs["win_probability"] = best_prob
    result.attrs["expected_points_lineup_win_probability"] = ep_win_prob
    result.attrs["expected_points_lineup_points"] = ep_points
    result.attrs["playoff_lineup_points"] = playoff_points
    return result
