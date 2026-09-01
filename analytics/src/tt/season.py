"""The season / championship simulator: `P(championship)`, and nothing else.

WHY THE OBJECTIVE IS THE TITLE AND NEVER "MAKING THE PLAYOFFS". Read from
the live league settings: `num_teams` 4, `num_playoff_teams` 4,
`playoff_start_week` 16, `end_week` 17, `uses_playoff_reseeding` 1,
`has_multiweek_championship` 0. EVERY TEAM MAKES THE PLAYOFFS. Playoff
probability is therefore identically 1.0 for all four teams and is not a
quantity any decision can be based on -- it is reported here
(`p_playoffs`) only so its uselessness is visible rather than assumed. The
regular season decides SEEDING and nothing else, and the title is settled
by two single-week, single-elimination games. Two consequences drive the
whole design:

  - Weeks 16-17 matter far more per week than any regular-season week. A
    regular-season week matters ONLY through its effect on seed, and in a
    4-of-4 league with reseeding it can turn out to matter not at all (see
    the `p**2` identity in test_season.py: whatever seed the strong team
    earns, it plays two opponents drawn from the same pool).
  - No parameter here is hardcoded to this league. `max_teams` is 10 and
    the league may fill, so team count, week numbers, playoff field size
    and round count are all read from the arguments; `run_bracket` handles
    any field size with byes, and `simulate_season` derives the number of
    rounds from `end_week - playoff_start_week + 1`.

PURE FUNCTION OF INJECTED DATA. No Yahoo calls, no file IO, no global
state: rosters, schedule and config come in as arguments and a DataFrame
goes out. Every test below builds its inputs by hand.

THE MODEL, AND EXACTLY WHERE IT IS A SIMPLIFICATION.

  1. A team's week score is `sum over starters of Normal(proj_points, sd)`,
     with every starter INDEPENDENT of every other. That is the identical
     model `playoff.win_probability` already documents and defends, reused
     rather than re-argued -- and `test_a_single_round_title_probability_
     matches_playoff_win_probability` pins that the two modules agree
     numerically on the same lineups, so the equivalence is verified, not
     asserted.

     WHICH WAY IT BIASES THE ANSWER, stated plainly. Teammates are
     positively correlated in reality (a QB and his own WR1 share one game
     script), so independence UNDERSTATES each team's weekly variance. In a
     single-elimination game `P(favourite wins) = Phi(dmu / sqrt(v1 + v2))`,
     which INCREASES as variance falls -- so this model OVERSTATES the
     favourite's championship probability and understates every underdog's.
     Partially offsetting it: the two sides of a matchup are also modelled
     as independent, while real teams' scores share a common NFL-week
     effect; positive cross-team correlation shrinks the variance of the
     MARGIN and would push the favourite's probability back up, so ignoring
     it biases the other way. Both are real gaps. Neither is hidden, and
     neither is fitted around.

  2. NORMAL SUMS ARE COMPUTED, NOT SAMPLED PER PLAYER. Under (1) a lineup's
     total is exactly `Normal(sum of means, sqrt(sum of variances))` --
     the Normal family is closed under summation -- so this module draws
     ONE number per (simulation, week, team) instead of one per starter.
     That is not an approximation: it is the same distribution, and it is
     the difference between a simulator that runs in half a second and one
     that runs in a minute. `team_week_moments` is where a roster becomes
     that (mean, sd) pair, and it reuses `lineup.optimal_lineup` and
     `playoff._starting_rows` so "who takes the field" cannot drift from
     the definition those modules already enforce.

  3. ONE WORLD PER WEEK. All teams' scores for simulation `i`, week `w` are
     drawn as one slice `scores[i, w, :]`, and both sides of every matchup
     in that week read from that slice. Drawing the two sides of a matchup
     separately would be statistically equivalent TODAY (the teams are
     independent), but it would make correlation impossible to add later
     without rewriting the loop, and it would make a team's own points-for
     depend on who it happened to play. `test_a_teams_week_score_does_not_
     depend_on_who_it_played` pins the property by re-pairing the same
     weeks and demanding bit-identical points-for. Adding a common weekly
     shock is a one-line change against this shape:
     `scores[:, w, :] += shock[:, w, None] * loading`.

  4. THE LINEUP IS SET ONCE, FROM EXPECTED POINTS, AND NEVER CHANGED.
     A manager sets a lineup BEFORE seeing outcomes, so choosing it inside
     the simulation from realised scores would be hindsight. Two honest
     consequences: (a) a real manager re-optimises weekly against new
     information, so a fixed lineup slightly understates every team;
     (b) `playoff.playoff_lineup`'s variance-aware choice is NOT used for
     the playoff rounds, because it needs a specific opponent and the
     opponent is itself a simulation outcome. Using the expected-points
     lineup throughout is the conservative choice: it is what an ordinary
     manager does, and it never credits a team with an optimisation it
     would have had to know the bracket to make.

  5. CONDITIONAL OR MARGINAL PROJECTIONS: `marginal=True` BY DEFAULT.
     `weekly.project_week` offers both views (see its docstring) and a
     caller who guesses wrong is badly misled, so the choice is an explicit
     argument. The default is the MARGINAL view -- the conditional
     distribution mixed with a point mass at zero for the weeks a player
     does not suit up -- for two reasons. A season simulation spans 15-17
     weeks and a starter WILL miss some of them; a certain zero from your
     RB1 is exactly the kind of discrete, high-variance event that decides
     a single-week playoff game, and the conditional view ("everyone plays
     every week") deletes it entirely. And the marginal view is the
     conservative one: it never inflates a team's score.

     WHAT THE MARGINAL VIEW STILL GETS WRONG, since it is not free: it
     charges a missed week at ZERO, when a real manager would start the
     best bench player instead. Truth is between the two views -- the
     conditional one assumes a costless replacement, the marginal one
     assumes no replacement at all. Modelling the real middle would mean
     re-running the lineup optimiser inside every simulated week for every
     realised availability pattern, which is the runtime this module
     exists to avoid. Pass `marginal=False` for the other bound; the
     truth is bracketed by the two runs.

  6. BYE WEEKS ARE NOT MODELLED. `weekly.py` says plainly that `p_active`
     cannot know the NFL schedule, and this module has no schedule to give
     it. So no team ever loses its whole QB slot to a bye. That understates
     weekly variance in the same direction as (1).

  7. K AND DEF SCORE ZERO. This pipeline projects offense only
     (`projections.PROJECTABLE_POSITIONS`), so a K or DEF slot has no
     projection to fill it and `lineup.py` names it an empty slot worth
     0.0. The `empty_slots` column in the output reports the count per team
     so the hole is visible rather than absorbed. It is not neutral: it
     removes real variance from every team, which again nudges the
     favourite up.

TIES.

  - REGULAR SEASON: a tied score is half a win to each side, matching
    Yahoo's own win-percentage arithmetic. With continuous scores this has
    probability zero and only ever fires in the deterministic (sd = 0)
    fixtures the tests are built on.
  - SEEDING: wins first, then POINTS FOR (Yahoo's default tiebreak once
    divisional records are out of play -- this league has no divisions),
    then the order teams appear in `rosters` as a final, fully deterministic
    backstop so two identical teams can never produce a run-to-run-varying
    bracket. `seed_order` is the single place this is decided.
  - PLAYOFFS: a tied playoff game advances the HIGHER SEED. `playoff.
    win_probability` counts a tie as a loss for its caller, which is the
    right conservative choice when there IS a caller; a bracket has no
    caller and needs a symmetric rule, and seed is the only ordering the
    bracket already knows. This differs from `win_probability` only on an
    event of probability zero whenever any starter has sd > 0.

DETERMINISM. `n` and `seed` alone determine every draw. All randomness is
consumed in exactly two `Generator.normal` calls, in a fixed order
(regular season, then playoffs), before any matchup or bracket logic runs
-- so the schedule and the bracket shape can change without disturbing the
random stream. Same arguments, same DataFrame, always.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import NamedTuple

import numpy as np
import pandas as pd

from .league import LeagueConfig
from .lineup import optimal_lineup
from .playoff import _starting_rows
from .weekly import for_lineup

# Default simulation count. Larger than `playoff.DEFAULT_N` (5000) because
# a whole season costs one Normal draw per (simulation, week, team) rather
# than a per-lineup Monte Carlo, so 10000 is still well under a second for
# this league's shape -- and the worst-case standard error of any reported
# probability, `1 / (2 * sqrt(n))`, is 0.005 there against 0.0071 at 5000.
DEFAULT_N = 10_000

# A bracket position with no team in it: the bye a top seed gets when the
# playoff field is not a power of two. Not a team index, never a champion,
# and given a score of -inf so it loses every game it is nominally in.
BYE = -1

_OUTPUT_COLUMNS = [
    "team", "championship_prob", "p_final", "expected_wins", "mean_points_for",
    "mean_seed", "p_seed_1", "p_playoffs", "exp_points_per_week", "sd_per_week",
    "empty_slots",
]


class BracketResult(NamedTuple):
    """`run_bracket`'s output.

    `champion` is one team index per simulation. `rounds` is one
    `(home, away, winner)` triple per round, each an integer array of shape
    `(simulations, games)` holding TEAM INDICES (or `BYE`), so a caller --
    or a test -- can inspect who actually played whom in each round rather
    than only who ended up holding the trophy. `home` is always the better
    seed of its pair, which is what makes "a tie advances the higher seed"
    a single `>=` rather than a lookup.
    """
    champion: np.ndarray
    rounds: list[tuple[np.ndarray, np.ndarray, np.ndarray]]


def round_robin_schedule(
    teams: Sequence[str], weeks: int, start_week: int = 1
) -> pd.DataFrame:
    """A balanced round-robin over `teams` for `weeks` weeks, in the shape
    `simulate_season` expects: rows of `(week, home_team, away_team)`.

    The circle method: one team is held fixed and the rest rotate, which
    guarantees every pairing is used exactly once before any is repeated
    (with `n` teams the cycle is `n - 1` weeks long, or `n` weeks for an odd
    `n`). An ODD number of teams gets a phantom opponent, so exactly one
    real team is idle each week -- a bye, expressed by that team simply
    having no row that week, which the simulator already handles (points-for
    accumulates per matchup, never per week).

    Home and away alternate by week so neither designation piles up on one
    team. Nothing in this module gives the home team any advantage; the
    columns exist because a real Yahoo schedule has them.
    """
    teams = list(teams)
    if len(teams) < 2:
        raise ValueError("a round robin needs at least 2 teams")
    if weeks < 1:
        raise ValueError("a schedule needs at least 1 week")

    slate: list[str | None] = list(teams)
    if len(slate) % 2:
        slate.append(None)
    size = len(slate)

    rows = []
    for offset in range(weeks):
        turn = offset % (size - 1)
        rotated = [slate[0]] + slate[1 + turn:] + slate[1:1 + turn]
        for i in range(size // 2):
            home, away = rotated[i], rotated[size - 1 - i]
            if home is None or away is None:
                continue
            if offset % 2:
                home, away = away, home
            rows.append({"week": start_week + offset, "home_team": home, "away_team": away})
    return pd.DataFrame(rows, columns=["week", "home_team", "away_team"])


def seed_order(wins: np.ndarray, points_for: np.ndarray) -> np.ndarray:
    """Team indices in seed order, best first, one row per simulation.

    THE TIEBREAK, in one place: wins, then points for, then the team's own
    index (i.e. the order it appeared in `rosters`). Yahoo's default
    standings tiebreak after win percentage is points for -- divisional
    record does not apply to this league, which has no divisions -- and the
    index is a final backstop so that two genuinely identical teams still
    produce a stable, reproducible bracket instead of whatever an unstable
    sort happens to settle on.

    Implemented as one `np.lexsort` over the last axis, so all `n`
    simulations are ranked in a single vectorised call. `lexsort` takes its
    PRIMARY key last, hence the reversed-looking tuple; negating a key sorts
    it descending.
    """
    wins = np.asarray(wins, dtype=float)
    points_for = np.asarray(points_for, dtype=float)
    if wins.shape != points_for.shape:
        raise ValueError("wins and points_for must have the same shape")
    order = np.broadcast_to(np.arange(wins.shape[-1]), wins.shape)
    return np.lexsort((order, -points_for, -wins), axis=-1)


def bracket_positions(size: int) -> list[int]:
    """Standard seeded bracket order for a field of `size` (a power of two):
    the SEED NUMBER occupying each bracket slot, so that adjacent slots meet
    in round one and the tree keeps the best seeds apart for as long as
    possible.

    Built by the usual mirroring recursion -- `[1]`, then `[1, 2]`, then
    `[1, 4, 2, 3]`, then `[1, 8, 4, 5, 2, 7, 3, 6]` -- each step pairing
    every seed `x` already placed with `2 * len + 1 - x`. Two properties the
    rest of the module leans on: the first element of every adjacent pair is
    always the BETTER seed (since `x < 2*len + 1 - x` for every `x` in the
    first half), and slot `i` of round `r + 1` is fed by slots `2i` and
    `2i + 1` of round `r`.
    """
    if size < 1 or size & (size - 1):
        raise ValueError(f"bracket size must be a power of two, got {size}")
    order = [1]
    while len(order) < size:
        mirror = 2 * len(order) + 1
        order = [value for x in order for value in (x, mirror - x)]
    return order


def _gather(scores: np.ndarray, teams: np.ndarray) -> np.ndarray:
    """Each named team's score, with `BYE` scoring -inf so it loses every
    game it is nominally part of (and, when two byes meet, still leaves a
    bye to be beaten by a real team in the next round).
    """
    padded = np.concatenate(
        [scores, np.full((scores.shape[0], 1), -np.inf)], axis=1
    )
    lookup = np.where(teams < 0, scores.shape[1], teams)
    return np.take_along_axis(padded, lookup, axis=1)


def run_bracket(
    seeds: np.ndarray, scores: np.ndarray, reseed: bool = True
) -> BracketResult:
    """Run a single-elimination bracket to a champion, vectorised over
    simulations.

    `seeds` is `(simulations, field)` team indices in seed order, best
    first. `scores` is `(simulations, rounds, teams)`: what each team scores
    in each playoff round. One round is one week -- this league's
    `has_multiweek_championship` is 0.

    RESEEDING (`uses_playoff_reseeding` in the league settings). With
    `reseed=True` the surviving teams are re-sorted by their original seed
    at the START of every round and re-laid into bracket order, so the best
    remaining seed always meets the worst remaining seed. With
    `reseed=False` winners simply propagate up a fixed tree. The two agree
    for a 4-team field (there is only one way to pair two survivors) and
    diverge from 6 teams up, which is why the test that pins the difference
    uses eight.

    A field smaller than `2 ** rounds` is padded with `BYE` slots placed at
    the worst seed positions, which is exactly how a real 6-team, 3-round
    bracket gives its top two seeds a first-round bye.
    """
    seeds = np.asarray(seeds)
    scores = np.asarray(scores, dtype=float)
    simulations, field = seeds.shape
    rounds = scores.shape[1]
    if scores.shape[0] != simulations:
        raise ValueError("seeds and scores disagree on the number of simulations")
    if rounds < 1:
        raise ValueError("a bracket needs at least 1 round")
    size = 1 << rounds
    if field > size:
        raise ValueError(
            f"a {rounds}-round bracket holds at most {size} teams, got {field}"
        )

    positions = bracket_positions(size)
    slots = np.full((simulations, size), BYE, dtype=int)
    for slot, seed_number in enumerate(positions):
        if seed_number <= field:
            slots[:, slot] = seeds[:, seed_number - 1]

    # Seed RANK per team, for the reseeding re-sort. A team outside the
    # field (and the BYE column at the end) ranks worse than every seed, so
    # it can never be sorted ahead of a real survivor.
    n_teams = scores.shape[2]
    rank = np.full((simulations, n_teams + 1), size + 1, dtype=int)
    np.put_along_axis(rank, seeds, np.arange(field)[None, :], axis=1)

    played: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
    for index in range(rounds):
        if reseed:
            width = slots.shape[1]
            lookup = np.where(slots < 0, n_teams, slots)
            by_seed = np.take_along_axis(rank, lookup, axis=1)
            ordered = np.take_along_axis(slots, np.argsort(by_seed, axis=1, kind="stable"), axis=1)
            layout = np.array(bracket_positions(width)) - 1
            slots = ordered[:, layout]

        home, away = slots[:, 0::2], slots[:, 1::2]
        round_scores = scores[:, index, :]
        # `>=` and not `>`: the first element of a bracket pair is always
        # the better seed (see `bracket_positions`), so this IS the "a tie
        # advances the higher seed" rule, with no separate branch.
        winner = np.where(_gather(round_scores, home) >= _gather(round_scores, away), home, away)
        played.append((home, away, winner))
        slots = winner

    return BracketResult(champion=slots[:, 0], rounds=played)


def _joined_roster(
    roster: pd.DataFrame,
    weekly: pd.DataFrame | None,
    marginal: bool,
    points_column: str,
    sd_column: str,
) -> pd.DataFrame:
    """A roster carrying the per-week mean and sd columns the lineup
    optimiser reads.

    Two accepted input shapes, because both are natural at a call site:
    a roster already joined to its projections (pass `weekly=None`), or a
    roster of bare `player_id`s plus the league-wide `weekly.project_week`
    board (pass it as `weekly`). In the second case the CONDITIONAL /
    MARGINAL choice is made here, once, through `weekly.for_lineup` -- the
    same explicit switch that module exists to force -- rather than by
    guessing which column a pre-joined frame meant.

    A roster player with no row in `weekly` (a kicker or defense, which
    this offense-only pipeline never projects) gets NaN points, and
    `lineup.py` already refuses to start a NaN-points player: the slot is
    named empty and scores zero. That is a documented hole, reported per
    team as `empty_slots`, not a silent one.
    """
    if weekly is None:
        return roster
    view = for_lineup(weekly, marginal=marginal)
    view = view.assign(**{points_column: view["proj_points"], sd_column: view["sd"]})
    wanted = ["player_id", points_column, sd_column]
    if "position" not in roster.columns:
        wanted.append("position")
    drop = [column for column in (points_column, sd_column) if column in roster.columns]
    return roster.drop(columns=drop).merge(view[wanted], on="player_id", how="left")


def team_week_moments(
    rosters: Mapping[str, pd.DataFrame],
    config: LeagueConfig,
    *,
    weekly: pd.DataFrame | None = None,
    marginal: bool = True,
    points_column: str = "proj_points",
    sd_column: str = "sd",
) -> pd.DataFrame:
    """Each team's ONE-WEEK score distribution: `team, exp_points, sd,
    empty_slots`.

    The lineup is `lineup.optimal_lineup`'s expected-points choice, made
    once (see the module docstring, point 4). Its total is
    `Normal(sum of means, sqrt(sum of variances))` under the independent-
    starter model -- VARIANCES add, standard deviations do not, and getting
    that wrong is the single easiest way to produce a simulator that looks
    plausible and is wrong by 40%.

    `playoff._starting_rows` decides which rows take the field, reused
    rather than reimplemented so that "starter, not benched, not an empty
    slot" means exactly one thing across the two modules. A missing or NaN
    sd is treated as 0.0 and a negative one is clipped to 0.0, matching
    `playoff._lineup_totals`'s own handling.
    """
    rows = []
    for team, roster in rosters.items():
        prepared = _joined_roster(roster, weekly, marginal, points_column, sd_column)
        if "position" not in prepared.columns:
            raise KeyError(f"roster for {team!r} has no 'position' column")
        lineup = optimal_lineup(prepared, config, points_column=points_column)
        starters = _starting_rows(lineup)
        mean = float(starters[points_column].fillna(0.0).sum()) if len(starters) else 0.0
        if len(starters) and sd_column in starters.columns:
            spread = np.clip(starters[sd_column].fillna(0.0).to_numpy(dtype=float), 0.0, None)
        else:
            spread = np.zeros(0)
        rows.append({
            "team": team,
            "exp_points": mean,
            "sd": float(np.sqrt((spread ** 2).sum())),
            "empty_slots": int(lineup["empty"].fillna(False).astype(bool).sum()),
        })
    return pd.DataFrame(rows, columns=["team", "exp_points", "sd", "empty_slots"])


def _validate_schedule(
    schedule: pd.DataFrame, teams: Mapping[str, int], playoff_start_week: int
) -> None:
    missing = [column for column in ("week", "home_team", "away_team")
               if column not in schedule.columns]
    if missing:
        raise KeyError(f"schedule is missing column(s) {missing}")
    for column in ("home_team", "away_team"):
        unknown = sorted(set(schedule[column]) - set(teams))
        if unknown:
            raise KeyError(f"schedule names team(s) with no roster: {unknown}")
    late = sorted(int(week) for week in schedule["week"] if week >= playoff_start_week)
    if late:
        # Silently dropping these would make a caller's regular season
        # quietly shorter than the one they wrote down.
        raise ValueError(
            f"schedule contains week(s) {late} at or after playoff_start_week "
            f"{playoff_start_week}; `schedule` is the REGULAR season only"
        )
    for week, games in schedule.groupby("week"):
        appearances = list(games["home_team"]) + list(games["away_team"])
        repeated = sorted({team for team in appearances if appearances.count(team) > 1})
        if repeated:
            raise ValueError(f"team(s) {repeated} play twice in week {int(week)}")


def simulate_season(
    rosters: Mapping[str, pd.DataFrame],
    schedule: pd.DataFrame,
    config: LeagueConfig,
    *,
    weekly: pd.DataFrame | None = None,
    playoff_start_week: int,
    end_week: int,
    playoff_teams: int | None = None,
    reseed: bool = True,
    marginal: bool = True,
    n: int = DEFAULT_N,
    seed: int | None = None,
    points_column: str = "proj_points",
    sd_column: str = "sd",
) -> pd.DataFrame:
    """Simulate the whole season `n` times and report each team's
    `championship_prob`, plus the diagnostics that make it checkable.

    ARGUMENTS
      `rosters`   team key -> that team's roster. Either already joined to
                  per-week projections (`proj_points` / `sd` columns) or
                  bare `player_id`s to be joined against `weekly`.
      `schedule`  the REGULAR season, rows of `(week, home_team,
                  away_team)`. A team with no row in some week simply has a
                  bye that week. A week at or after `playoff_start_week` is
                  an error, not something to silently drop.
      `config`    the league's roster slots (what a legal lineup is).
      `weekly`    optional `weekly.project_week` output to join onto the
                  rosters; see `_joined_roster`.
      `playoff_start_week` / `end_week`
                  read from the league settings, never assumed. The number
                  of single-elimination rounds is
                  `end_week - playoff_start_week + 1` (this league:
                  16 -> 17, so two).
      `playoff_teams`
                  size of the playoff field; `None` means EVERY team, which
                  is this league's actual setting (4 of 4).
      `reseed`    `uses_playoff_reseeding`. Defaults to True, the live
                  setting.
      `marginal`  which `weekly` view to use; see the module docstring,
                  point 5. Only meaningful when `weekly` is given.
      `n`, `seed` simulation count and the seed that makes the answer
                  reproducible. `n` is the accuracy/speed dial: the
                  worst-case standard error of any probability below is
                  `1 / (2 * sqrt(n))`, reported as
                  `result.attrs["monte_carlo_se"]`.

    RETURNS one row per team, sorted by `championship_prob` descending:

      championship_prob    P(wins the title). THE objective.
      p_final              P(reaches the last round).
      expected_wins        mean regular-season wins (a tie is half a win).
      mean_points_for      mean regular-season points scored.
      mean_seed            mean playoff seed (1 is best).
      p_seed_1             P(top seed) -- what a regular-season week is
                           actually worth, since seeding is all it decides.
      p_playoffs           P(makes the field). 1.0 for every team in this
                           league, reported so that is visible.
      exp_points_per_week  the starting lineup's mean weekly points.
      sd_per_week          that lineup's weekly standard deviation.
      empty_slots          starting slots this roster cannot fill (K and
                           DEF always, in this pipeline).
    """
    if end_week < playoff_start_week:
        raise ValueError(
            f"end_week {end_week} is before playoff_start_week {playoff_start_week}"
        )
    if n < 1:
        raise ValueError("n must be at least 1")

    team_keys = list(rosters)
    n_teams = len(team_keys)
    if n_teams < 2:
        raise ValueError("a season needs at least 2 teams")
    index = {team: i for i, team in enumerate(team_keys)}

    field = n_teams if playoff_teams is None else int(playoff_teams)
    if not 1 <= field <= n_teams:
        raise ValueError(
            f"playoff_teams {field} must be between 1 and the {n_teams} teams in the league"
        )
    rounds = end_week - playoff_start_week + 1
    if field > (1 << rounds):
        raise ValueError(
            f"playoff_teams {field} cannot be reduced to a champion in {rounds} "
            f"round(s) (weeks {playoff_start_week}-{end_week})"
        )

    _validate_schedule(schedule, index, playoff_start_week)

    moments = team_week_moments(
        rosters, config, weekly=weekly, marginal=marginal,
        points_column=points_column, sd_column=sd_column,
    ).set_index("team").loc[team_keys]
    mean = moments["exp_points"].to_numpy(dtype=float)
    spread = moments["sd"].to_numpy(dtype=float)

    weeks = sorted(int(week) for week in pd.unique(schedule["week"]))
    week_index = {week: i for i, week in enumerate(weeks)}

    # ALL randomness, consumed here, in this order, before any matchup or
    # bracket logic -- see the module docstring on determinism, and on
    # "one world per week" (a week is a slice of the first array, shared by
    # both sides of every matchup in it).
    rng = np.random.default_rng(seed)
    regular = rng.normal(mean, spread, size=(n, len(weeks), n_teams))
    postseason = rng.normal(mean, spread, size=(n, rounds, n_teams))

    wins = np.zeros((n, n_teams))
    points_for = np.zeros((n, n_teams))
    for row in schedule.itertuples():
        week = week_index[int(row.week)]
        home, away = index[row.home_team], index[row.away_team]
        home_score = regular[:, week, home]
        away_score = regular[:, week, away]
        points_for[:, home] += home_score
        points_for[:, away] += away_score
        # A tie is half a win to each side, matching Yahoo's win percentage.
        home_wins = (home_score > away_score).astype(float)
        drawn = (home_score == away_score).astype(float)
        wins[:, home] += home_wins + 0.5 * drawn
        wins[:, away] += (1.0 - home_wins - drawn) + 0.5 * drawn

    seeds = seed_order(wins, points_for)
    rank = np.empty((n, n_teams), dtype=int)
    np.put_along_axis(rank, seeds, np.arange(1, n_teams + 1)[None, :], axis=1)

    bracket = run_bracket(seeds[:, :field], postseason, reseed=reseed)

    championships = np.bincount(bracket.champion, minlength=n_teams)[:n_teams] / n
    finalists = np.zeros(n_teams)
    last_home, last_away, _ = bracket.rounds[-1]
    for side in (last_home, last_away):
        present = side[side >= 0]
        finalists += np.bincount(present, minlength=n_teams)[:n_teams] / n

    result = pd.DataFrame({
        "team": team_keys,
        "championship_prob": championships,
        "p_final": finalists,
        "expected_wins": wins.mean(axis=0),
        "mean_points_for": points_for.mean(axis=0),
        "mean_seed": rank.mean(axis=0),
        "p_seed_1": (rank == 1).mean(axis=0),
        "p_playoffs": (rank <= field).mean(axis=0),
        "exp_points_per_week": mean,
        "sd_per_week": spread,
        "empty_slots": moments["empty_slots"].to_numpy(dtype=int),
    }, columns=_OUTPUT_COLUMNS)
    result = result.sort_values(
        "championship_prob", ascending=False, kind="mergesort"
    ).reset_index(drop=True)

    result.attrs.update({
        "n": n,
        "seed": seed,
        "regular_season_weeks": len(weeks),
        "playoff_rounds": rounds,
        "playoff_start_week": playoff_start_week,
        "end_week": end_week,
        "playoff_teams": field,
        "reseed": reseed,
        "marginal": marginal,
        "monte_carlo_se": 0.5 / math.sqrt(n),
    })
    return result
