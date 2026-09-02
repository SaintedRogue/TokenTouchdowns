"""Positional draft strategy: WHEN to take each position, measured.

`studies.draft_board` answered "which RANKING rule should sort the board"
(consensus ADP vs pure VOR vs VOR-with-survival) and found pure VOR
unusable. This module answers the orthogonal question a drafter actually
asks at the table -- "it's round 3, do I need a running back yet?" -- with
the SAME out-of-sample instrument: projections fit on seasons strictly
before S, drafted on season S's own preseason ADP, graded on the ACTUAL
season-S regular-season points the drafted players went on to score, with a
non-appearing player scoring ZERO.

HOW THE ARMS ARE BUILT, AND WHY IT MATTERS. Every arm here is the SAME
base strategy (`mock.strategy_adp` -- take the best player left by
consensus ADP) wrapped in a positional CONSTRAINT. Nothing else differs:
not the ranking, not the opponents, not the seeds. So a difference between
"RB-first" and "Zero-RB" is attributable to the positional timing rule and
to nothing else. Racing eight bespoke drafters that also disagreed about
how to rank players within a position would confound the two questions and
answer neither.

THE RUNWAY GUARD, AND WHY EVERY ARM CARRIES IT. Pure ADP is roster-blind.
Measured on the real 2024 board, 25 drafts per cell: a bare `strategy_adp`
drafter finished with NO tight end in 11/25 four-team drafts and 5/25
ten-team drafts, and no quarterback in 2/25 four-team drafts. Graded on an
optimal starting lineup, an unfilled slot scores zero -- so ANY "draft a TE
early" arm would beat that baseline by a mile, and the result would be
"remember to draft a tight end," not "tight ends are worth taking early."
`starter_runway_positions` closes that: once a roster's outstanding
mandatory starter count (`draft.roster_need`, this league's own slots) is
about to exceed the rounds it has left, the board is restricted to
positions it still needs. It is applied IDENTICALLY to every arm,
including the BPA baseline, and by construction it cannot fire early --
with 15 rounds and 6 mandatory starters it only binds in the last handful
of rounds. It removes a roster-management artifact; it does not express a
positional opinion.

CONSTRAINT PRECEDENCE (`constrained_strategy`). Three primitives compose
every arm below:

  `only_in_rounds`  round -> the ONLY positions allowed that round
                    (RB-first is `{1: {"RB"}, 2: {"RB"}}`)
  `forbid_before`   position -> first round it may be taken
                    (Zero-RB is `{"RB": 5}`)
  `require_by`      position -> round by which one must be on the roster
                    (early-QB is `{"QB": 3}`)

They are applied in that order, then the runway guard, and a `require_by`
whose deadline has arrived OVERRIDES an earlier `forbid_before` -- a plan
that both bans and demands the same position is contradictory, and
satisfying the demand is the only reading under which the arm still means
what its name says. If the resulting allowed set matches NO player left on
the board, the constraint is dropped for that pick rather than raising:
a strategy that cannot pick at all would abort the whole trial, and
"there were no tight ends left" is a fact about the board, not a bug.

WHAT `value_lost_by_waiting` MEASURES. For each of my picks, for each
position: the ACTUAL points of the best-by-consensus-ADP player available
at that position NOW, minus the actual points of the best one available at
my NEXT pick. Availability at the next pick adds MY OWN picks back onto
the board (see `_waiting_cost_rows`) -- the counterfactual is "if I DON'T
take him now, is he still there when I pick again," which is a question
about what the OTHER teams do, exactly as `survival.add_survival` models
it. Leaving my own pick out would score the tautology "the player I just
drafted is no longer available."

Points come from `actual_points` (real season-S scoring), never from
`proj_points`: a cliff measured on this project's own projections would
just be a picture of this project's own projections.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..draft import roster_need
from ..league import LeagueConfig
from ..mock import (
    ADP_NOISE_DEFAULT,
    DEFAULT_ROUNDS,
    Strategy,
    # Private, deliberately: the snake-draft pick arithmetic is already
    # written and tested once in `mock`, and a second copy here is exactly
    # the drift this project's own "one implementation" rule exists to
    # prevent (see `mock.optimal_lineup_score`'s note on the same choice).
    _pick_number,
    compare_strategies,
    simulate_draft,
    strategy_adp,
)
from ..vor import add_vor, replacement_levels
from .draft_board import actual_lineup_score

# Positions this pipeline projects and this league starts. Mirrors
# `projections.PROJECTABLE_POSITIONS`, restated here as the fixed column
# order every table in this module reports, so a reader always sees the
# four positions in the same order.
POSITIONS: tuple[str, ...] = ("QB", "RB", "WR", "TE")


def round_of(pick: int, teams: int) -> int:
    """1-indexed draft round containing 1-indexed overall `pick`."""
    if pick < 1:
        raise ValueError(f"pick must be 1-indexed (>= 1), got {pick}")
    if teams < 1:
        raise ValueError(f"teams must be at least 1, got {teams}")
    return (pick - 1) // teams + 1


def starter_runway_positions(
    roster: list[dict], config: LeagueConfig, current_round: int, rounds: int,
) -> set[str] | None:
    """Positions this roster MUST spend the pick on, or None if it has slack.

    Returns None -- meaning "no constraint, draft whatever the arm wants" --
    whenever the rounds remaining (including this one) strictly exceed the
    number of mandatory starter slots still unfilled. Only when the roster
    is down to exactly enough picks to fill its lineup does this restrict
    the board to the positions it still owes.

    See the module docstring for why every arm carries this: without it a
    roster-blind ADP baseline finishes with an empty lineup slot in 20-44%
    of drafts, and every "take this position early" arm wins on that
    artifact alone rather than on positional value.
    """
    need = roster_need(roster, config)
    outstanding = {position: count for position, count in need.items() if count > 0}
    # K/DEF are never projected (see projections.PROJECTABLE_POSITIONS), so
    # they can never be drafted from this board and must not consume runway.
    outstanding = {p: c for p, c in outstanding.items() if p in POSITIONS}
    total = sum(outstanding.values())
    if total == 0:
        return None
    rounds_left = rounds - current_round + 1
    if rounds_left > total:
        return None
    return set(outstanding)


@dataclass(frozen=True)
class PositionalPlan:
    """A declarative positional-timing rule -- see the module docstring's
    CONSTRAINT PRECEDENCE section for how the three fields interact."""

    only_in_rounds: Mapping[int, frozenset[str]] = field(default_factory=dict)
    forbid_before: Mapping[str, int] = field(default_factory=dict)
    require_by: Mapping[str, int] = field(default_factory=dict)

    def allowed(
        self, current_round: int, roster: list[dict], available: Iterable[str],
    ) -> set[str] | None:
        """Positions this plan permits at `current_round`, or None for "any".

        `available` is the set of positions actually left on the board; it
        is what an empty intersection is measured against by the caller,
        not filtered here.
        """
        allowed: set[str] | None = None
        window = self.only_in_rounds.get(current_round)
        if window is not None:
            allowed = set(window)
        if self.forbid_before:
            banned = {
                position for position, first in self.forbid_before.items()
                if current_round < first
            }
            if banned:
                base = set(available) if allowed is None else allowed
                allowed = base - banned
        overdue = self._overdue(current_round, roster)
        if overdue:
            # Deadline overrides an earlier ban -- see module docstring.
            allowed = overdue
        return allowed

    def _overdue(self, current_round: int, roster: list[dict]) -> set[str]:
        held = {entry["position"] for entry in roster}
        return {
            position for position, deadline in self.require_by.items()
            if current_round >= deadline and position not in held
        }


def constrained_strategy(
    plan: PositionalPlan,
    config: LeagueConfig,
    rounds: int,
    base: Strategy = strategy_adp,
    enforce_runway: bool = True,
) -> Strategy:
    """Wrap `base` so it may only pick positions `plan` permits this round
    (and, with `enforce_runway`, positions the roster still owes a starter).

    The wrapper never chooses a player itself -- it filters the board and
    hands the filtered board to `base`, which keeps "how to rank players"
    entirely in the base strategy and "when to take which position"
    entirely here (module docstring, HOW THE ARMS ARE BUILT).

    A filter that would empty the board is DROPPED for that pick (see
    module docstring): the runway constraint is tried first, then the
    plan's, then the unfiltered board.
    """

    def _pick(board: pd.DataFrame, roster: list[dict], pick: int, next_pick: int, teams: int) -> dict:
        if board.empty:
            raise ValueError("constrained_strategy: no players remain on the board")
        current_round = round_of(pick, teams)
        available = set(board["position"].unique())

        allowed = plan.allowed(current_round, roster, available)
        if enforce_runway:
            runway = starter_runway_positions(roster, config, current_round, rounds)
            if runway is not None:
                # The runway guard is a hard roster-legality requirement, so
                # it wins over the arm's own preference -- but only where the
                # two can both be satisfied.
                both = runway if allowed is None else (allowed & runway)
                allowed = both if _matches(board, both) else runway

        if allowed is not None and _matches(board, allowed):
            return base(board[board["position"].isin(allowed)], roster, pick, next_pick, teams)
        return base(board, roster, pick, next_pick, teams)

    return _pick


def _matches(board: pd.DataFrame, positions: set[str]) -> bool:
    return bool(positions) and bool(board["position"].isin(positions).any())


# The eight arms. Every one is `strategy_adp` under a positional
# constraint; `bpa` is the unconstrained control (identical to
# `draft_board`'s own `adp` arm apart from the runway guard every arm
# carries). Round numbers are the ones the brief names, not values chosen
# by trying several and keeping whichever won -- nothing in this module is
# tuned against the outcome it measures.
PLANS: dict[str, PositionalPlan] = {
    "bpa": PositionalPlan(),
    "rb_first": PositionalPlan(only_in_rounds={1: frozenset({"RB"}), 2: frozenset({"RB"})}),
    "wr_first": PositionalPlan(only_in_rounds={1: frozenset({"WR"}), 2: frozenset({"WR"})}),
    "zero_rb": PositionalPlan(forbid_before={"RB": 5}),
    "early_qb": PositionalPlan(require_by={"QB": 3}),
    "late_qb": PositionalPlan(forbid_before={"QB": 8}),
    "early_te": PositionalPlan(require_by={"TE": 3}),
    "late_te": PositionalPlan(forbid_before={"TE": 8}),
}


def strategies_for(
    config: LeagueConfig, rounds: int = DEFAULT_ROUNDS, base: Strategy = strategy_adp,
) -> dict[str, Strategy]:
    """Every arm in `PLANS`, built against this league and draft depth."""
    return {
        name: constrained_strategy(plan, config, rounds, base=base)
        for name, plan in PLANS.items()
    }


def run_positional_cell(
    board: pd.DataFrame,
    config: LeagueConfig,
    season: int,
    teams: int,
    my_slot: int,
    trials: int,
    seed: int,
    actual_points: pd.Series,
    rounds: int = DEFAULT_ROUNDS,
) -> pd.DataFrame:
    """One (season, teams) cell: race every arm in `strategies_for` on
    ACTUAL season-`season` points.

    Deliberately the same shape as `draft_board.run_backtest_cell` --
    same `compare_strategies` call, same `actual_lineup_score` grading,
    same common-random-numbers seeding -- so the two studies' numbers are
    directly comparable and neither has its own private scoring rule.
    """
    scorer = actual_lineup_score(actual_points, config)
    result = compare_strategies(
        board, strategies_for(config, rounds), trials=trials, teams=teams,
        my_slot=my_slot, seed=seed, rounds=rounds, score_roster=scorer, config=config,
    )
    result.insert(0, "season", season)
    result.insert(1, "teams", teams)
    return result


def roster_composition(
    board: pd.DataFrame,
    config: LeagueConfig,
    teams: int,
    my_slot: int,
    seed: int,
    rounds: int = DEFAULT_ROUNDS,
    trials: int = 25,
) -> pd.DataFrame:
    """Mean positional counts per drafted roster, and how often a mandatory
    starter slot finished EMPTY, per arm.

    This is the diagnostic that justifies the runway guard (module
    docstring) and the check that it worked: an arm whose `empty_slots` is
    non-zero is being graded on an unfilled lineup, and any edge it shows
    is partly roster management rather than positional value. Trial seeds
    are `compare_strategies`' own sequence for this `seed`, so these are
    the same drafts the scores came from.
    """
    trial_seeds = np.random.default_rng(seed).integers(0, 2**31 - 1, size=trials).tolist()
    targets = {p: c for p, c in roster_need([], config).items() if p in POSITIONS}
    rows = []
    for name, strategy in strategies_for(config, rounds).items():
        counts = {position: 0 for position in POSITIONS}
        empty = 0
        for trial_seed in trial_seeds:
            drafted = simulate_draft(
                board, teams, rounds, my_slot, strategy, seed=int(trial_seed),
                opponent_strategy=strategy_adp,
            )
            held = drafted["position"].value_counts()
            for position in POSITIONS:
                counts[position] += int(held.get(position, 0))
            empty += sum(
                max(0, target - int(held.get(position, 0)))
                for position, target in targets.items()
            )
        rows.append({
            "strategy": name, "trials": trials,
            **{position: counts[position] / trials for position in POSITIONS},
            "empty_slots": empty / trials,
        })
    return pd.DataFrame(rows)


def _waiting_cost_rows(
    log: pd.DataFrame,
    board: pd.DataFrame,
    teams: int,
    rounds: int,
    my_slot: int,
    actual_points: pd.Series,
) -> list[dict]:
    """Per-round, per-position value lost by waiting one turn, from ONE
    draft's full pick log (`simulate_draft(..., return_all=True)`).

    See the module docstring for the counterfactual this implements. The
    "who would I take" ordering is the board's raw consensus `adp`, not the
    per-draft `_effective_adp` noise -- that noise models the OTHER teams
    deviating from consensus, and is already reflected in which players the
    log says are gone; applying it to my own choice too would be modelling
    me as misreading my own board.
    """
    ranked = board[board["adp"].notna()].sort_values("adp")
    points = ranked["player_id"].map(actual_points).fillna(0.0).to_numpy()
    positions = ranked["position"].to_numpy()
    ids = ranked["player_id"].to_numpy()

    pick_of = dict(zip(log["player_id"], log["pick_number"], strict=True))
    slot_of = dict(zip(log["player_id"], log["slot"], strict=True))
    taken_at = np.array([pick_of.get(pid, np.inf) for pid in ids], dtype=float)
    mine = np.array([slot_of.get(pid, -1) == my_slot for pid in ids], dtype=bool)

    rows = []
    for round_index in range(rounds - 1):
        pick = _pick_number(teams, round_index, my_slot)
        next_pick = _pick_number(teams, round_index + 1, my_slot)
        now = taken_at >= pick
        # My own picks from this round onward are added BACK: the question
        # is whether the OTHER teams take him while I wait.
        later = (taken_at >= next_pick) | (mine & (taken_at >= pick))
        for position in POSITIONS:
            at_position = positions == position
            value_now = _best(points, now & at_position)
            value_next = _best(points, later & at_position)
            if value_now is None or value_next is None:
                continue
            rows.append({
                "round": round_index + 1, "position": position,
                "value_now": value_now, "value_next": value_next,
                "value_lost": value_now - value_next,
            })
    return rows


def _best(points: np.ndarray, mask: np.ndarray) -> float | None:
    """Actual points of the FIRST player (best consensus ADP) under `mask`;
    None when nobody at that position is left."""
    index = np.flatnonzero(mask)
    return float(points[index[0]]) if len(index) else None


def value_lost_by_waiting(
    board: pd.DataFrame,
    config: LeagueConfig,
    teams: int,
    my_slot: int,
    seed: int,
    actual_points: pd.Series,
    rounds: int = DEFAULT_ROUNDS,
    trials: int = 200,
    strategy: Strategy | None = None,
    adp_noise: float = ADP_NOISE_DEFAULT,
) -> pd.DataFrame:
    """`trials` drafts' worth of per-round, per-position waiting cost --
    one row per (trial, round, position), for the caller to aggregate.

    Trial seeds are `compare_strategies`' own sequence for this `seed`, so
    these are the same simulated draft nights the scores came from.

    `adp_noise` is exposed (rather than left at `simulate_draft`'s default)
    only so a test can pin an EXACT, hand-computable draft order; the real
    study leaves it at `ADP_NOISE_DEFAULT`, the same value every other
    simulation in this project uses.
    """
    picker = strategy if strategy is not None else constrained_strategy(
        PLANS["bpa"], config, rounds,
    )
    trial_seeds = np.random.default_rng(seed).integers(0, 2**31 - 1, size=trials).tolist()
    rows = []
    for trial, trial_seed in enumerate(trial_seeds):
        log = simulate_draft(
            board, teams, rounds, my_slot, picker, seed=int(trial_seed),
            adp_noise=adp_noise, opponent_strategy=strategy_adp, return_all=True,
        )
        for row in _waiting_cost_rows(log, board, teams, rounds, my_slot, actual_points):
            rows.append({"trial": trial, **row})
    return pd.DataFrame(rows)


def points_by_position(
    board: pd.DataFrame,
    config: LeagueConfig,
    teams: int,
    actual_points: pd.Series | None = None,
) -> pd.DataFrame:
    """Raw points vs value over replacement, side by side, per position.

    One row per position, on the SAME board, so the disagreement VOR exists
    to express is a subtraction the reader can check rather than a claim:
    `best_points` and `replacement_points` are the level, `best_vor` is the
    margin, and the two orderings differ.

    With `actual_points` given, the same three quantities are recomputed
    from REAL season points -- players re-ranked within position by what
    they actually scored -- so a reader can see whether the projections'
    positional story survives contact with the season. That column set is
    an ex-post description of a completed season, NOT something a drafter
    could have known; it exists to check the projections, not to draft on.
    """
    scored = add_vor(board, config, teams=teams)
    # `vor.replacement_levels`, not a second copy of its arithmetic: this
    # table's `replacement_rank` must be the rank `add_vor` actually
    # subtracted at, or `best_vor` and `best_points - replacement_points`
    # would silently disagree (they are asserted equal in the tests).
    levels = replacement_levels(config, teams)
    rows = []
    for position in POSITIONS:
        group = scored[(scored["position"] == position) & scored["proj_points"].notna()]
        if group.empty:
            continue
        level = max(1, min(levels.get(position, 1), len(group)))
        ordered = group.sort_values("proj_points", ascending=False)
        row = {
            "position": position,
            "replacement_rank": level,
            "best_points": float(ordered.iloc[0]["proj_points"]),
            "replacement_points": float(ordered.iloc[level - 1]["proj_points"]),
            "best_vor": float(ordered.iloc[0]["vor"]),
            "starter_mean_points": float(ordered.iloc[:level]["proj_points"].mean()),
            "starter_mean_vor": float(ordered.iloc[:level]["vor"].mean()),
        }
        if actual_points is not None:
            real = group["player_id"].map(actual_points).fillna(0.0).sort_values(ascending=False)
            row["actual_best_points"] = float(real.iloc[0])
            row["actual_replacement_points"] = float(real.iloc[level - 1])
            row["actual_best_vor"] = float(real.iloc[0] - real.iloc[level - 1])
            row["actual_starter_mean_points"] = float(real.iloc[:level].mean())
            row["actual_starter_mean_vor"] = float(
                real.iloc[:level].mean() - real.iloc[level - 1]
            )
        rows.append(row)
    return pd.DataFrame(rows)


def run_positional_study(
    boards: Mapping[tuple[int, int], pd.DataFrame],
    config: LeagueConfig,
    actual_by_season: Mapping[int, pd.Series],
    trials: int = 400,
    seed: int = 2026,
    rounds: int = DEFAULT_ROUNDS,
    my_slot_for_teams: Callable[[int], int] | None = None,
    on_cell: Callable[[int, int, pd.DataFrame, pd.DataFrame, pd.DataFrame], None] | None = None,
) -> pd.DataFrame:
    """Race every arm over every pre-built `(season, teams) -> board` cell.

    Boards are built by the CALLER and passed in, exactly once per cell --
    the same rule `draft_board.run_backtest`'s own WARNING states, for the
    same reason (`project_players` redraws its Monte Carlo sample on every
    call, so a board rebuilt per-consumer silently differs from the one
    actually drafted against).

    `my_slot_for_teams` defaults to `teams // 2`, matching the existing
    backtest's fixed, untuned middle-of-the-order choice.

    `on_cell(season, teams, scores, composition, waiting)` fires after each
    cell so a driver script can cache partial results to disk; the two
    diagnostics are computed ONLY when it is given.
    """
    slot_for = my_slot_for_teams if my_slot_for_teams is not None else (lambda teams: teams // 2)
    rows = []
    for (season, teams), board in boards.items():
        actual = actual_by_season[season]
        slot = slot_for(teams)
        cell = run_positional_cell(
            board, config, season, teams, slot, trials, seed, actual, rounds=rounds,
        )
        rows.append(cell)
        if on_cell is not None:
            composition = roster_composition(
                board, config, teams, slot, seed=seed, rounds=rounds, trials=trials,
            )
            waiting = value_lost_by_waiting(
                board, config, teams, slot, seed=seed, actual_points=actual,
                rounds=rounds, trials=trials,
            )
            for frame in (composition, waiting):
                frame.insert(0, "season", season)
                frame.insert(1, "teams", teams)
            on_cell(season, teams, cell, composition, waiting)
    return pd.concat(rows, ignore_index=True)
