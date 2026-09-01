"""Trade valuation: what a trade does to CHAMPIONSHIP PROBABILITY, for
BOTH sides, measured on paired simulations.

WHY THE ANSWER IS NOT POINTS. A trade is worth the change it makes to the
probability you win the title, and in this league that is emphatically not
the same as the change in projected points. Read from the live settings:
`num_teams` 4, `num_playoff_teams` 4, `playoff_start_week` 16, `end_week`
17, `uses_playoff_reseeding` 1. EVERY TEAM QUALIFIES, so the fifteen-week
regular season buys nothing but a seed, and the title is settled by two
single-week games. `tt.season` already measured how little the seed is
worth on real rosters: P(seed 1) ranged 0.18-0.36 across four drafted
teams while championship probability ranged only 0.22-0.29. Weeks 16-17
dominate. Nothing in this module hand-weights that -- it falls out of
simulating the whole season and reading the title column, which is the
only reason the weighting can be trusted.

TWO SIDES, ALWAYS. `trade_ratify_type` is "vote": the league votes trades
through, so a proposal that is wonderful for you and terrible for the
other manager is not a trade, it is a rejected message. `evaluate_trade`
therefore returns a row per side and `find_trades` reports the
counterparty's delta beside your own. Mutually beneficial trades are not
a courtesy -- they are the only kind that get accepted, and they genuinely
exist because rosters have different holes. `tt.season`'s realistic run
found the pathology in a real drafted roster: the team with the HIGHEST
13-man season projection had the LOWEST title odds, because it had
stockpiled four quarterbacks whose points can never enter a one-QB
starting lineup.

------------------------------------------------------------------------
COMMON RANDOM NUMBERS: THE ONE THING THIS MODULE MUST NOT GET WRONG
------------------------------------------------------------------------
A championship probability from `n` simulations carries a standard error
of about `0.5 / sqrt(n)` -- 0.0022 at n = 50000. Plenty of real trades
move the title by less than that. Simulating "before" and "after" as two
independent runs and subtracting would report the difference of two noisy
numbers, whose standard error is LARGER than either; at that point most of
what you are ranking is the random number generator.

So both runs use the SAME simulated worlds. Every draw here is
`mean + sd * z` where `z` is a standard normal array drawn ONCE, from the
seed, and reused unchanged for the before roster and the after roster.
`numpy` computes `Generator.normal(loc, scale, size)` as exactly
`loc + scale * standard_normal(size)` (verified bit-for-bit, and pinned by
`test_the_before_column_reproduces_simulate_season_to_the_last_digit`),
which is what lets this module hold the draws still while the rosters move
and still agree with `tt.season.simulate_season` to the last digit.

The delta is then a PAIRED statistic. Per simulated world `i`,
`D_i = 1{champion after == me} - 1{champion before == me}`, taking values
in {-1, 0, +1}; the reported `delta` is its mean and `delta_se` its
sample standard error `sd(D) / sqrt(n)`. Because a trade that changes a
roster slightly changes the champion in only a few worlds, `D` is zero
almost everywhere and `delta_se` is typically 3-10x smaller than the
unpaired difference's standard error. Two consequences worth stating:

  - A NO-OP TRADE MEASURES EXACTLY ZERO. Not "small": zero, with a
    standard error of zero, at every `n`. Two tests demand exactly that
    (an empty trade, and a player swapped for an identical twin), and they
    are the tests that pin the paired machinery -- under independent draws
    both would be nonzero.
  - A TRADE INSIDE THE NOISE IS LABELLED SO. `significant` is
    `|delta| > 1.96 * delta_se`, and `delta_ci_low` / `delta_ci_high`
    are reported so a caller can see the interval rather than trust a
    flag. `worlds_gained` and `worlds_lost` -- the raw counts of worlds
    whose champion changed -- are reported too, because they are what the
    standard error is computed from and they make it checkable by hand.

`delta` is computed as `(worlds_gained - worlds_lost) / n` rather than as
the difference of the two reported probabilities. Those are the same
number in exact arithmetic; the count form is used because it is exactly
antisymmetric between the two teams of a two-team league, so their deltas
sum to exactly 0.0 instead of to a rounding error.

------------------------------------------------------------------------
WHAT IS REUSED, AND WHAT IS DELIBERATELY RE-IMPLEMENTED
------------------------------------------------------------------------
Lineups come from `lineup.optimal_lineup` through `season.team_week_
moments`; seeding from `season.seed_order`; the bracket from
`season.run_bracket`; the conditional/marginal projection choice from
`season._joined_roster`, which routes through `weekly.for_lineup`. None of
that is rebuilt here.

The one thing that IS re-implemented is `simulate_season`'s outer loop
(`_SeasonDraws`), for two reasons that cannot be met by calling it:
`simulate_season` aggregates the per-world champion away, and this module
needs it to compute a paired statistic; and it re-draws its random numbers
on every call, while a search over hundreds of candidate trades needs one
fixed set of worlds held across all of them. The re-implementation is not
allowed to become a second model:
`test_the_before_column_reproduces_simulate_season_to_the_last_digit`
asserts exact equality with `simulate_season` on the same seed and `n`,
for championship probability, expected wins, P(seed 1) and the weekly
moments. If the two ever diverge, that test fails.

------------------------------------------------------------------------
ROSTER CONSTRUCTION IS THE MECHANISM -- NOT A POSITIONAL-NEED BONUS
------------------------------------------------------------------------
Trading your third-best RB for their second-best WR can raise your title
odds even when the RB is the better player, because you only start so
many. That effect is already fully present: `optimal_lineup` decides who
takes the field, and a player who cannot crack the lineup contributes
exactly zero to `exp_points`. NO positional-need heuristic is layered on
top anywhere in this module -- doing so would count the same effect twice.
The `find_trades` screen (below) uses lineup values, never position
labels.

Both sides are re-optimised. After the swap the counterparty sets its own
best lineup too, which is what `team_week_moments` does when it is handed
the post-trade roster. Evaluating a trade against the counterparty's
PRE-trade lineup would overstate your gain and understate theirs;
`test_the_counterparty_re_optimises_its_own_lineup_after_the_swap` pins
the post-trade number to a hand-computed level that neither of the two
wrong answers can produce.

------------------------------------------------------------------------
WHAT THIS MODEL DOES NOT PRICE
------------------------------------------------------------------------
BENCH DEPTH IS WORTH EXACTLY ZERO HERE, and that is a real gap, not a
rounding one. `season`'s model sets one lineup from expected points and
never re-sets it inside a simulated week, so a backup who would step in
when a starter is out never steps in. Under the default marginal view a
missed week is charged at zero points for the starter and the bench is not
consulted. A fourth WR in a one-WR league therefore moves nothing at all
(`test_a_bench_player_adds_nothing_to_the_modelled_lineup_strength` pins
that exactly). The hook for fixing it exists -- `weekly.project_week`
exposes `p_active` per player -- but using it means re-running the lineup
optimiser inside every simulated week for every realised availability
pattern, which is precisely the cost `season` was built to avoid. Until
then: this module systematically UNDERVALUES depth and OVERVALUES a
top-heavy roster, and a human reading its output should discount a trade
that hollows out a bench accordingly.

Inherited from `season`, and stated there in full: starters are
independent (understates weekly variance, so it overstates the
favourite), byes are not modelled, and K/DEF score zero. A trade for a
kicker is invisible to this module.
"""
from __future__ import annotations

import itertools
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import NamedTuple

import numpy as np
import pandas as pd

from .league import LeagueConfig
from .season import (
    DEFAULT_N,
    _joined_roster,
    _validate_schedule,
    run_bracket,
    seed_order,
    team_week_moments,
)

__all__ = [
    "DEFAULT_N",
    "Z_95",
    "apply_trade",
    "evaluate_trade",
    "find_trades",
]

# Two-sided 95% normal quantile. Used for the reported interval on the
# delta and for the `significant` flag, so both mean the same thing.
Z_95 = 1.959963984540054

_SIDE_COLUMNS = [
    "team", "role", "gives", "gets", "give_names", "get_names",
    "championship_prob_before", "championship_prob_after",
    "delta", "delta_se", "delta_ci_low", "delta_ci_high", "significant",
    "worlds_gained", "worlds_lost",
    "exp_points_before", "exp_points_after", "exp_points_delta",
    "sd_before", "sd_after",
    "playoff_win_prob_before", "playoff_win_prob_after", "playoff_win_prob_delta",
    "expected_wins_before", "expected_wins_after", "expected_wins_delta",
    "p_seed_1_before", "p_seed_1_after", "p_seed_1_delta",
]

# Stage 1's two screens. "pairwise" prices every (drop, get) PAIR with the
# real optimiser and is exact on a one-for-one; "additive" prices players
# one at a time against the roster as it stands, which is linear rather than
# quadratic and which MISSES mutually beneficial packages (see
# `find_trades`). The default is "pairwise" because the cheap screen's
# failure mode is a confident "no trades available" when dozens exist.
_SCREEN_MODES = ("pairwise", "additive")

_FIND_COLUMNS = [
    "their_team", "gives", "gets", "give_names", "get_names",
    "my_delta", "my_delta_se", "my_delta_ci_low", "my_delta_ci_high", "my_significant",
    "their_delta", "their_delta_se", "their_delta_ci_low", "their_delta_ci_high",
    "their_significant", "mutual",
    "my_exp_points_delta", "their_exp_points_delta",
    "my_playoff_win_prob_delta", "their_playoff_win_prob_delta",
    "my_championship_prob_before", "my_championship_prob_after",
    "their_championship_prob_before", "their_championship_prob_after",
    "screen_score",
]


def _norm_cdf(x: float) -> float:
    """Standard normal CDF, from `math.erf` -- so this module carries no
    scipy dependency for one closed form that `math` already has exactly.
    """
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ----------------------------------------------------------------------
# The swap
# ----------------------------------------------------------------------

def apply_trade(
    rosters: Mapping[str, pd.DataFrame],
    my_team: str,
    their_team: str,
    i_give: Iterable[str],
    i_get: Iterable[str],
    *,
    id_column: str = "player_id",
) -> dict[str, pd.DataFrame]:
    """`rosters` with the named players moved across, as a NEW dict of NEW
    frames -- the inputs are never mutated.

    TEAM ORDER IS PRESERVED EXACTLY. `simulate_season` indexes teams by
    `list(rosters)` and this module's common random numbers are indexed the
    same way, so a reordering here would silently re-pair every team with a
    different column of the random draws and quietly break the pairing that
    the whole module rests on.

    Departing players are removed and arriving players appended, so a
    roster's surviving rows keep their relative order. `lineup.py` breaks
    exact ties in projected points by original row order (its documented,
    stable-sort tiebreak), so an arriving player who ties a survivor to the
    last decimal will lose that tie -- the only way row order can matter.

    Both directions may be empty (an empty trade is a legitimate no-op, and
    the test that it measures exactly zero is what pins the paired-draw
    machinery); what is not allowed is naming a player his team does not
    hold, naming one twice, or trading with yourself.
    """
    for team in (my_team, their_team):
        if team not in rosters:
            raise KeyError(f"no roster for team {team!r}")
    if my_team == their_team:
        raise ValueError(f"team {my_team!r} cannot trade with itself")

    give, get = list(i_give), list(i_get)
    for label, names in (("i_give", give), ("i_get", get)):
        duplicated = sorted({name for name in names if names.count(name) > 1})
        if duplicated:
            raise ValueError(f"{label} names player(s) {duplicated} twice")
    both = sorted(set(give) & set(get))
    if both:
        raise ValueError(f"player(s) {both} appear on both sides of the trade")

    mine, theirs = rosters[my_team], rosters[their_team]
    for team, roster, names in ((my_team, mine, give), (their_team, theirs, get)):
        held = set(roster[id_column])
        missing = sorted(set(names) - held)
        if missing:
            raise KeyError(f"team {team!r} does not hold player(s) {missing}")

    out = dict(rosters)
    out[my_team] = _swapped(mine, theirs, give, get, id_column)
    out[their_team] = _swapped(theirs, mine, get, give, id_column)
    return out


def _swapped(
    roster: pd.DataFrame, other: pd.DataFrame,
    out_ids: Sequence[str], in_ids: Sequence[str], id_column: str,
) -> pd.DataFrame:
    kept = roster[~roster[id_column].isin(out_ids)]
    arriving = other[other[id_column].isin(in_ids)]
    return pd.concat([kept, arriving], ignore_index=True, sort=False)


# ----------------------------------------------------------------------
# The simulation core, with the draws held still
# ----------------------------------------------------------------------

class _Outcome(NamedTuple):
    """One run of the season over the held-still draws."""
    champion: np.ndarray      # (n,) team index
    wins: np.ndarray          # (n, teams)
    rank: np.ndarray          # (n, teams) seed, 1 is best


class _SeasonDraws:
    """`simulate_season`'s loop with the random numbers pulled out front.

    Draws `z` once and exposes `run(mean, sd)`, so any number of rosters
    can be evaluated against IDENTICAL simulated worlds. That is the whole
    mechanism behind the paired delta -- see the module docstring.

    The draw order and shapes match `simulate_season` exactly (regular
    season first, then the playoff rounds), which is what makes
    `mean + sd * z` here identical to its `rng.normal(mean, sd, size)`
    there for the same seed.
    """

    def __init__(
        self,
        teams: Sequence[str],
        schedule: pd.DataFrame,
        *,
        playoff_start_week: int,
        end_week: int,
        playoff_teams: int | None,
        reseed: bool,
        n: int,
        seed: int | None,
    ) -> None:
        if end_week < playoff_start_week:
            raise ValueError(
                f"end_week {end_week} is before playoff_start_week {playoff_start_week}"
            )
        if n < 1:
            raise ValueError("n must be at least 1")
        self.teams = list(teams)
        self.n_teams = len(self.teams)
        if self.n_teams < 2:
            raise ValueError("a season needs at least 2 teams")
        self.index = {team: i for i, team in enumerate(self.teams)}

        self.field = self.n_teams if playoff_teams is None else int(playoff_teams)
        if not 1 <= self.field <= self.n_teams:
            raise ValueError(
                f"playoff_teams {self.field} must be between 1 and the "
                f"{self.n_teams} teams in the league"
            )
        self.rounds = end_week - playoff_start_week + 1
        if self.field > (1 << self.rounds):
            raise ValueError(
                f"playoff_teams {self.field} cannot be reduced to a champion in "
                f"{self.rounds} round(s) (weeks {playoff_start_week}-{end_week})"
            )
        _validate_schedule(schedule, self.index, playoff_start_week)

        weeks = sorted(int(week) for week in pd.unique(schedule["week"]))
        week_index = {week: i for i, week in enumerate(weeks)}
        self.games = [
            (week_index[int(row.week)], self.index[row.home_team], self.index[row.away_team])
            for row in schedule.itertuples()
        ]
        self.n = n
        self.reseed = reseed
        rng = np.random.default_rng(seed)
        self._z_regular = rng.standard_normal((n, len(weeks), self.n_teams))
        self._z_playoff = rng.standard_normal((n, self.rounds, self.n_teams))

    def run(self, mean: np.ndarray, sd: np.ndarray) -> _Outcome:
        regular = mean + sd * self._z_regular
        postseason = mean + sd * self._z_playoff

        wins = np.zeros((self.n, self.n_teams))
        points_for = np.zeros((self.n, self.n_teams))
        for week, home, away in self.games:
            home_score = regular[:, week, home]
            away_score = regular[:, week, away]
            points_for[:, home] += home_score
            points_for[:, away] += away_score
            home_wins = (home_score > away_score).astype(float)
            drawn = (home_score == away_score).astype(float)
            wins[:, home] += home_wins + 0.5 * drawn
            wins[:, away] += (1.0 - home_wins - drawn) + 0.5 * drawn

        seeds = seed_order(wins, points_for)
        rank = np.empty((self.n, self.n_teams), dtype=int)
        np.put_along_axis(rank, seeds, np.arange(1, self.n_teams + 1)[None, :], axis=1)
        bracket = run_bracket(seeds[:, :self.field], postseason, reseed=self.reseed)
        return _Outcome(champion=bracket.champion, wins=wins, rank=rank)


# ----------------------------------------------------------------------
# Roster -> weekly moments
# ----------------------------------------------------------------------

def _prepare(
    rosters: Mapping[str, pd.DataFrame],
    weekly: pd.DataFrame | None,
    marginal: bool,
    points_column: str,
    sd_column: str,
) -> dict[str, pd.DataFrame]:
    """Every roster joined to its per-week projection, once.

    Routed through `season._joined_roster` rather than re-merged here so
    the CONDITIONAL / MARGINAL choice has exactly one definition in the
    codebase (`weekly.for_lineup`'s), and a roster that arrives already
    joined is passed through untouched.
    """
    return {
        team: _joined_roster(roster, weekly, marginal, points_column, sd_column)
        for team, roster in rosters.items()
    }


def _moments(
    prepared: Mapping[str, pd.DataFrame],
    teams: Sequence[str],
    config: LeagueConfig,
    points_column: str,
    sd_column: str,
) -> tuple[np.ndarray, np.ndarray]:
    """`(mean, sd)` per team, in `teams` order, from the shared optimiser."""
    frame = team_week_moments(
        {team: prepared[team] for team in teams}, config, weekly=None,
        points_column=points_column, sd_column=sd_column,
    ).set_index("team").loc[list(teams)]
    return (
        frame["exp_points"].to_numpy(dtype=float),
        frame["sd"].to_numpy(dtype=float),
    )


def _playoff_win_prob(mean: np.ndarray, sd: np.ndarray) -> np.ndarray:
    """Each team's chance of winning ONE single-elimination week against an
    average league opponent: the mean over the other teams of
    `Phi((mu_i - mu_j) / sqrt(var_i + var_j))`.

    This is what "playoff-week lineup strength" means in a format whose
    title is decided by two single-week games. It is deliberately NOT a
    second lineup: `season`'s model sets one expected-points lineup and
    uses it in every week, playoff weeks included (a variance-aware playoff
    lineup would need to know the opponent, and the opponent is itself a
    simulation outcome). What changes between the regular season and week
    16 is not the lineup, it is how that lineup's mean and spread convert
    into an outcome -- and this is that conversion, reported so a caller
    can see it move.

    With no variance anywhere the ratio is undefined; the deterministic
    answer (1, 0 or a coin flip on an exact tie) is used instead.
    """
    n_teams = len(mean)
    out = np.zeros(n_teams)
    for i in range(n_teams):
        others = []
        for j in range(n_teams):
            if i == j:
                continue
            spread = math.sqrt(sd[i] ** 2 + sd[j] ** 2)
            if spread == 0.0:
                others.append(1.0 if mean[i] > mean[j] else 0.5 if mean[i] == mean[j] else 0.0)
            else:
                others.append(_norm_cdf((mean[i] - mean[j]) / spread))
        out[i] = float(np.mean(others)) if others else 0.0
    return out


class _Paired(NamedTuple):
    delta: float
    se: float
    ci_low: float
    ci_high: float
    significant: bool
    gained: int
    lost: int


def _paired_delta(before: np.ndarray, after: np.ndarray, team: int, n: int) -> _Paired:
    """The paired statistic for one team, from the two champion arrays.

    `D_i = 1{after == team} - 1{before == team}` in {-1, 0, +1}. Summarised
    from the two flip COUNTS rather than from the array, because those
    counts are reported and this keeps the reported standard error exactly
    recomputable from the reported columns (which is what
    `test_the_standard_error_is_the_paired_one_computed_from_the_flip_
    counts` checks).
    """
    won_before = before == team
    won_after = after == team
    gained = int(np.count_nonzero(won_after & ~won_before))
    lost = int(np.count_nonzero(won_before & ~won_after))
    total = float(gained - lost)
    delta = total / n
    if n > 1:
        # sum(D^2) = gained + lost and sum(D) = gained - lost, so the sample
        # variance needs nothing but the counts.
        variance = max(((gained + lost) - total ** 2 / n) / (n - 1), 0.0)
        se = math.sqrt(variance / n)
    else:
        se = 0.0
    return _Paired(
        delta=delta, se=se,
        ci_low=delta - Z_95 * se, ci_high=delta + Z_95 * se,
        significant=abs(delta) > Z_95 * se,
        gained=gained, lost=lost,
    )


def _names(roster: pd.DataFrame, ids: Sequence[str], id_column: str) -> str:
    if not len(ids):
        return ""
    if "name" not in roster.columns:
        return " + ".join(ids)
    lookup = roster.set_index(id_column)["name"]
    return " + ".join(str(lookup.get(pid, pid)) for pid in ids)


# ----------------------------------------------------------------------
# evaluate_trade
# ----------------------------------------------------------------------

def evaluate_trade(
    rosters: Mapping[str, pd.DataFrame],
    schedule: pd.DataFrame,
    config: LeagueConfig,
    *,
    my_team: str,
    their_team: str,
    i_give: Iterable[str],
    i_get: Iterable[str],
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
    id_column: str = "player_id",
) -> pd.DataFrame:
    """Simulate the season before and after one specific swap, on the SAME
    worlds, and report what it does to BOTH teams' title odds.

    ARGUMENTS beyond `season.simulate_season`'s (which all mean exactly what
    they mean there):
      `my_team` / `their_team`  the two sides.
      `i_give` / `i_get`        player ids leaving / arriving, from
                                `my_team`'s point of view. Either may be
                                empty; an empty trade measures exactly zero
                                and is the module's own control.

    RETURNS two rows -- `my_team` first, then `their_team`:

      role                     "proposer" / "counterparty".
      gives / gets             what that side sends and receives.
      championship_prob_before/_after
                               THE objective, on the same worlds.
      delta                    after minus before. A trade IS this number.
      delta_se                 the PAIRED standard error (module docstring).
      delta_ci_low/_high       delta +- 1.96 se.
      significant              |delta| > 1.96 se. False means "this trade
                               is inside the noise", which is an answer.
      worlds_gained/_lost      simulated seasons whose champion changed to
                               / away from this team. `delta_se` is a
                               closed form of just these and `n`.
      exp_points_before/_after/_delta, sd_before/_after
                               the starting lineup's weekly mean and spread,
                               re-optimised on both sides after the swap.
      playoff_win_prob_*       one single-elimination week against an
                               average opponent (see `_playoff_win_prob`).
      expected_wins_*, p_seed_1_*
                               the regular season's share, reported so its
                               smallness is visible rather than assumed.

    `result.attrs` carries `n`, `seed`, the unpaired `monte_carlo_se` for
    comparison, and the trade itself.
    """
    give, get = list(i_give), list(i_get)
    after_rosters = apply_trade(rosters, my_team, their_team, give, get, id_column=id_column)

    teams = list(rosters)
    draws = _SeasonDraws(
        teams, schedule, playoff_start_week=playoff_start_week, end_week=end_week,
        playoff_teams=playoff_teams, reseed=reseed, n=n, seed=seed,
    )
    before_prepared = _prepare(rosters, weekly, marginal, points_column, sd_column)
    after_prepared = _prepare(after_rosters, weekly, marginal, points_column, sd_column)
    mean_before, sd_before = _moments(before_prepared, teams, config, points_column, sd_column)
    mean_after, sd_after = _moments(after_prepared, teams, config, points_column, sd_column)

    before = draws.run(mean_before, sd_before)
    after = draws.run(mean_after, sd_after)
    result = _side_rows(
        draws, before, after, rosters, my_team, their_team, give, get,
        mean_before, sd_before, mean_after, sd_after, id_column,
    )
    result.attrs.update({
        "n": n, "seed": seed,
        "monte_carlo_se": 0.5 / math.sqrt(n),
        "my_team": my_team, "their_team": their_team,
        "i_give": tuple(give), "i_get": tuple(get),
        "marginal": marginal, "reseed": reseed,
        "playoff_start_week": playoff_start_week, "end_week": end_week,
    })
    return result


def _side_rows(
    draws: _SeasonDraws,
    before: _Outcome,
    after: _Outcome,
    rosters: Mapping[str, pd.DataFrame],
    my_team: str,
    their_team: str,
    give: Sequence[str],
    get: Sequence[str],
    mean_before: np.ndarray,
    sd_before: np.ndarray,
    mean_after: np.ndarray,
    sd_after: np.ndarray,
    id_column: str,
) -> pd.DataFrame:
    playoff_before = _playoff_win_prob(mean_before, sd_before)
    playoff_after = _playoff_win_prob(mean_after, sd_after)
    rows = []
    for team, role, sends, receives in (
        (my_team, "proposer", give, get),
        (their_team, "counterparty", get, give),
    ):
        i = draws.index[team]
        paired = _paired_delta(before.champion, after.champion, i, draws.n)
        rows.append({
            "team": team, "role": role,
            "gives": tuple(sends), "gets": tuple(receives),
            "give_names": _names(rosters[team], sends, id_column),
            "get_names": _names(
                rosters[their_team if team == my_team else my_team], receives, id_column
            ),
            "championship_prob_before": float((before.champion == i).mean()),
            "championship_prob_after": float((after.champion == i).mean()),
            "delta": paired.delta, "delta_se": paired.se,
            "delta_ci_low": paired.ci_low, "delta_ci_high": paired.ci_high,
            "significant": paired.significant,
            "worlds_gained": paired.gained, "worlds_lost": paired.lost,
            "exp_points_before": float(mean_before[i]),
            "exp_points_after": float(mean_after[i]),
            "exp_points_delta": float(mean_after[i] - mean_before[i]),
            "sd_before": float(sd_before[i]), "sd_after": float(sd_after[i]),
            "playoff_win_prob_before": float(playoff_before[i]),
            "playoff_win_prob_after": float(playoff_after[i]),
            "playoff_win_prob_delta": float(playoff_after[i] - playoff_before[i]),
            "expected_wins_before": float(before.wins[:, i].mean()),
            "expected_wins_after": float(after.wins[:, i].mean()),
            "expected_wins_delta": float(after.wins[:, i].mean() - before.wins[:, i].mean()),
            "p_seed_1_before": float((before.rank[:, i] == 1).mean()),
            "p_seed_1_after": float((after.rank[:, i] == 1).mean()),
            "p_seed_1_delta": float(
                (after.rank[:, i] == 1).mean() - (before.rank[:, i] == 1).mean()
            ),
        })
    return pd.DataFrame(rows, columns=_SIDE_COLUMNS)


# ----------------------------------------------------------------------
# find_trades
# ----------------------------------------------------------------------

def _subsets(ids: Sequence[str], max_size: int) -> list[tuple[str, ...]]:
    return [
        combo
        for size in range(1, max_size + 1)
        for combo in itertools.combinations(ids, size)
    ]


def _swap_value(
    prepared: pd.DataFrame, donor: pd.DataFrame, out_ids: Sequence[str],
    in_ids: Sequence[str], config: LeagueConfig, points_column: str,
    sd_column: str, id_column: str,
) -> float:
    roster = _swapped(prepared, donor, out_ids, in_ids, id_column)
    frame = team_week_moments(
        {"_": roster}, config, weekly=None,
        points_column=points_column, sd_column=sd_column,
    )
    return float(frame["exp_points"].iloc[0])


def _marginal_tables(
    prepared: Mapping[str, pd.DataFrame], my_team: str, their_team: str,
    config: LeagueConfig, points_column: str, sd_column: str, id_column: str,
) -> tuple[dict, dict, dict, dict]:
    """Per-player lineup values, for the screen.

    Four one-player questions, answered with the real optimiser:
      `my_drop[p]`    what MY lineup loses if p leaves my roster
      `my_add[p]`     what MY lineup gains if p (theirs) joins mine
      `their_drop[p]` what THEIR lineup loses if p leaves theirs
      `their_add[p]`  what THEIR lineup gains if p (mine) joins theirs

    That is 2 * (my roster + their roster) optimiser calls per counterparty
    -- linear in roster size. These four numbers alone are what the
    ADDITIVE screen is built from, and combining them additively is what
    made it miss package trades (see `find_trades`); the default screen
    uses them only for the players a package leaves unmatched, and prices
    the matched ones exactly with `_pair_table`.
    """
    mine, theirs = prepared[my_team], prepared[their_team]
    base_mine = _swap_value(mine, theirs, [], [], config, points_column, sd_column, id_column)
    base_theirs = _swap_value(theirs, mine, [], [], config, points_column, sd_column, id_column)
    my_ids = list(mine[id_column])
    their_ids = list(theirs[id_column])
    my_drop = {
        pid: base_mine - _swap_value(mine, theirs, [pid], [], config, points_column, sd_column, id_column)
        for pid in my_ids
    }
    my_add = {
        pid: _swap_value(mine, theirs, [], [pid], config, points_column, sd_column, id_column) - base_mine
        for pid in their_ids
    }
    their_drop = {
        pid: base_theirs - _swap_value(theirs, mine, [pid], [], config, points_column, sd_column, id_column)
        for pid in their_ids
    }
    their_add = {
        pid: _swap_value(theirs, mine, [], [pid], config, points_column, sd_column, id_column) - base_theirs
        for pid in my_ids
    }
    return my_drop, my_add, their_drop, their_add


def _pair_table(
    prepared: pd.DataFrame, donor: pd.DataFrame,
    out_ids: Sequence[str], in_ids: Sequence[str],
    config: LeagueConfig, points_column: str, sd_column: str, id_column: str,
) -> dict[tuple[str, str], float]:
    """`mu(R - d + g) - mu(R)` for EVERY (drop, get) PAIR, from the real
    optimiser -- the exact value of that one-for-one swap, not an estimate
    of it.

    This is the whole fix. `_marginal_tables` asks what one player is worth
    ARRIVING AT or LEAVING a roster that still holds everyone else, and no
    arithmetic on those four numbers can know that an arriving wide
    receiver steps into the slot the departing one vacated. Asking the
    optimiser the two-player question instead costs
    `len(out_ids) * len(in_ids)` calls -- quadratic in roster size where
    `_marginal_tables` is linear (about 8 ms a call, so ~1.4 s per side per
    counterparty on a 13-man roster) -- and it gets the answer right by
    construction rather than by bound.
    """
    base = _swap_value(prepared, donor, [], [], config, points_column, sd_column, id_column)
    return {
        (out_id, in_id): _swap_value(
            prepared, donor, [out_id], [in_id],
            config, points_column, sd_column, id_column,
        ) - base
        for out_id in out_ids
        for in_id in in_ids
    }


class _ScreenTables(NamedTuple):
    """Everything stage 1 needs to score any candidate against ONE
    counterparty. `my_pair` / `their_pair` are empty in the additive mode,
    which is exactly what `_package_value` switches on.
    """
    my_drop: dict[str, float]
    my_add: dict[str, float]
    their_drop: dict[str, float]
    their_add: dict[str, float]
    my_pair: dict[tuple[str, str], float]
    their_pair: dict[tuple[str, str], float]


def _screen_tables(
    prepared: Mapping[str, pd.DataFrame], my_team: str, their_team: str,
    config: LeagueConfig, screen_mode: str,
    points_column: str, sd_column: str, id_column: str,
) -> _ScreenTables:
    mine, theirs = prepared[my_team], prepared[their_team]
    my_ids = list(mine[id_column])
    their_ids = list(theirs[id_column])
    my_drop, my_add, their_drop, their_add = _marginal_tables(
        prepared, my_team, their_team, config, points_column, sd_column, id_column,
    )
    if screen_mode == "additive":
        return _ScreenTables(my_drop, my_add, their_drop, their_add, {}, {})
    return _ScreenTables(
        my_drop, my_add, their_drop, their_add,
        _pair_table(mine, theirs, my_ids, their_ids,
                    config, points_column, sd_column, id_column),
        _pair_table(theirs, mine, their_ids, my_ids,
                    config, points_column, sd_column, id_column),
    )


def _best_decomposition(
    outs: Sequence[str], ins: Sequence[str],
    drop: Mapping[str, float], add: Mapping[str, float],
    pair: Mapping[tuple[str, str], float],
    index: int = 0, used: int = 0,
) -> float:
    """Best total over every way of MATCHING departing players to arriving
    ones, relative to a roster that has already been credited `sum(add)`.

    Each departing player is either matched to a still-unmatched arrival --
    worth `pair[out, in] - add[in]`, i.e. the exact swap value in place of
    the arrival's already-counted solo value -- or left unmatched, worth
    `-drop[out]`. An arrival nobody is matched to keeps its solo `add`.
    `used` is a bitmask over `ins`, so a package of p for q explores
    `sum_k C(p,k) C(q,k) k!` combinations: 7 for a 2-for-2, 34 for a
    3-for-3. Exponential in package size and irrelevant beside one season
    simulation.

    Leaving a pair unmatched is always an option, so this is never below the
    additive score -- the pairwise screen can only ever PROMOTE what the
    additive one demoted, which is the property `find_trades` needs.
    """
    if index == len(outs):
        return 0.0
    out_id = outs[index]
    best = -drop[out_id] + _best_decomposition(
        outs, ins, drop, add, pair, index + 1, used
    )
    for position, in_id in enumerate(ins):
        bit = 1 << position
        if used & bit:
            continue
        value = pair[out_id, in_id] - add[in_id] + _best_decomposition(
            outs, ins, drop, add, pair, index + 1, used | bit
        )
        best = max(best, value)
    return best


def _package_value(
    outs: Sequence[str], ins: Sequence[str],
    drop: Mapping[str, float], add: Mapping[str, float],
    pair: Mapping[tuple[str, str], float],
) -> float:
    """One side's stage-1 score for sending `outs` and receiving `ins`.

    With no pair table (the additive mode) this is the old surrogate,
    `sum(add) - sum(drop)`: every player priced against the roster as it
    stands, which charges the full loss of a starter even when an arrival
    replaces him.

    With one, it is the best decomposition of the package into EXACT
    one-for-one swaps plus unmatched solo moves. A one-for-one is then
    exact outright -- `pair[d, g]` IS the trade -- and a package is exact
    whenever its parts pair off cleanly, which is the case the additive
    screen got wrong by a whole starter's worth of points.
    """
    additive = sum(add[in_id] for in_id in ins) - sum(drop[out_id] for out_id in outs)
    if not pair:
        return additive
    if len(outs) == 1 and len(ins) == 1:
        return max(additive, pair[outs[0], ins[0]])
    return sum(add[in_id] for in_id in ins) + _best_decomposition(outs, ins, drop, add, pair)


def find_trades(
    rosters: Mapping[str, pd.DataFrame],
    schedule: pd.DataFrame,
    config: LeagueConfig,
    *,
    my_team: str,
    max_give: int = 2,
    max_get: int = 2,
    their_teams: Sequence[str] | None = None,
    weekly: pd.DataFrame | None = None,
    playoff_start_week: int,
    end_week: int,
    playoff_teams: int | None = None,
    reseed: bool = True,
    marginal: bool = True,
    n: int = DEFAULT_N,
    seed: int | None = None,
    screen_top: int | None = 40,
    screen_mode: str = "pairwise",
    points_column: str = "proj_points",
    sd_column: str = "sd",
    id_column: str = "player_id",
    drop_dominated: bool = True,
) -> pd.DataFrame:
    """Search for trades that raise MY championship probability, ranked --
    and report the counterparty's delta beside every one, because a trade
    the other manager will not accept is not a trade.

    ---------------------------------------------------------------
    THE PRUNING STRATEGY, AND WHAT IT CAN MISS
    ---------------------------------------------------------------
    The candidate space is combinatorial: with 13-man rosters, three
    counterparties and packages of up to two players each way, there are
    (13 + 78) ** 2 * 3 = 24843 candidate trades. Each costs a full
    before/after season pair, so simulating all of them takes minutes. The
    search is therefore a two-stage funnel, and BOTH stages are stated
    here because a silent heuristic is worse than a bad one.

    STAGE 1 -- A PAIRWISE-EXACT LINEUP-VALUE SCREEN (`screen_mode`
    "pairwise", the default). `_pair_table` asks the real optimiser the
    TWO-player question for every (departing, arriving) PAIR:
    `mu(R - d + g) - mu(R)`, which IS the value of that one-for-one swap,
    not an estimate of it. `_marginal_tables` supplies the one-player
    values for whatever a package leaves unmatched. A candidate's score is
    then the best decomposition of the package into those exact swaps
    (`_package_value`):

        my_hat    = max over matchings of (my exact swaps + unmatched solos)
        their_hat = the same from the counterparty's side

    so a ONE-FOR-ONE IS SCORED EXACTLY -- `screen_score` equals the
    simulated `my_exp_points_delta` to the last digit -- and a package is
    scored exactly whenever its parts pair off cleanly, which is the case
    the old additive screen got wrong by a whole starter's worth of points.
    Candidates are ranked twice -- by `my_hat` (best for me) and by
    `min(my_hat, their_hat)` (best mutual) -- and the top `screen_top` of
    EACH list is kept, so a lopsided trade that is merely good for me and a
    balanced one that is good for both are both carried forward. The screen
    never looks at a position label: it is lineup value throughout, so it
    does not double-count the positional-scarcity effect the optimiser
    already prices.

    THE COST is `2 * len(mine) * len(theirs)` optimiser calls per
    counterparty -- QUADRATIC in roster size where the additive screen is
    linear. Measured on this project's 13-man rosters at about 8 ms a call:
    ~2.8 s per counterparty, so ~8 s of stage 1 for the real four-team
    league and ~25 s for a ten-team one (`max_teams` is 10). That is the
    price of the screen telling the truth about a package, and it is paid
    once per counterparty however many candidates there are.

    WHY THE DEFAULT IS NOT THE CHEAPER SCREEN. `screen_mode="additive"`
    keeps the old surrogate -- 2 * (13 + 13) = 52 calls per counterparty,
    linear -- which scores each player against the roster AS IT STANDS:

        my_hat    = sum(my_add[got])      - sum(my_drop[given])
        their_hat = sum(their_add[given]) - sum(their_drop[got])

    Lineup value is submodular, so both those scores are LOWER BOUNDS on
    the true change in weekly starting points: the screen can only DEMOTE a
    good trade, never promote a bad one. But it demotes packages very hard,
    because it charges the full loss of a starter even when an arriving
    player steps into the slot he vacated. MEASURED, on this project's four
    real drafted rosters: the best mutually beneficial 2-for-2 available to
    the QB-hoarding team is worth +0.94 points a week to it and +0.68 to
    the counterparty; the additive screen scores those +0.92 and MINUS
    0.77 -- the counterparty's score has the WRONG SIGN -- ranking the
    trade 1503rd of 8281 on the mutual key, so `screen_top=40` never saw
    it and the search reported ZERO mutually beneficial trades where an
    exhaustive one finds 49. THAT WAS A SILENT WRONG ANSWER TO THIS
    FUNCTION'S HEADLINE QUESTION, which is why it is no longer the
    default. The pairwise screen scores the same trade at exactly
    +0.94 / +0.68 and finds the mutual set.
    `test_the_default_screen_finds_the_mutual_package_the_additive_screen_
    missed` pins both halves of that on a hand-built fixture.
    `screen_mode="additive"` remains available for a league large enough
    that the quadratic table hurts, and it is the wrong tool for mutual
    discovery over packages whenever it is used.

    WHAT THE PAIRWISE SCREEN STILL CANNOT SEE, all real:
      1. IT IS EXACT ON SWAPS, NOT ON PACKAGES. A package is scored by its
         best pairing, so it is exact when the parts pair off and only
         approximate when they do not -- two arrivals competing for one
         slot are each credited with displacing the same incumbent
         (overstated), and two departures freeing one slot likewise. Unlike
         the additive screen it is NOT a bound in either direction; it is
         an estimate that is exact on the one-for-one case and, on the real
         rosters, on the package it exists to find. Overstating merely
         spends a simulation, which stage 2 then reports correctly.
      2. IT IS A MEAN-ONLY SCREEN. `playoff.py` documents that variance
         helps an underdog: a trade that lowers your weekly mean while
         raising your spread can raise a weak team's title odds, and this
         screen would never simulate it. Trades that win through variance
         are invisible to stage 1, in either mode.
      3. IT PRICES NO DEPTH, because nothing in this module does (see the
         module docstring).

    THE ESCAPE HATCH IS REAL: `screen_top=None` disables stage 1's
    selection entirely and simulates EVERY enumerated candidate. That is
    exact whatever the rosters look like, it is the way to measure what the
    heuristic costs on a given league (it is how the failure above was
    measured), and it is what to reach for when the answer has to be
    complete rather than fast -- 812 s for the real league's 24843
    two-for-two candidates at n = 20000, against 11 s at the default. The
    screen score is still computed and reported in that mode, so a caller
    can always compare stage 1 against the simulated truth.

    STAGE 2 -- the survivors get the full paired treatment: one season
    simulation per candidate, on the SAME worlds as the baseline and as
    each other, so every reported delta is comparable and every one carries
    its own paired standard error. The baseline is simulated ONCE and
    reused, so a candidate costs one simulation, not two.

    OTHER ARGUMENTS
      `max_give` / `max_get`  package size caps. Both directions are always
                              non-empty: a one-sided gift is not a trade,
                              and this league ratifies by vote.
      `their_teams`           which counterparties to consider; every other
                              team by default.
      `n`                     the accuracy/speed dial, passed straight
                              through. Cost is roughly
                              `candidates * n * weeks * teams` flops.
      `screen_top`            how many candidates survive stage 1 on EACH
                              of its two ranking keys, per counterparty.
                              `None` disables the selection and simulates
                              everything (above).
      `screen_mode`           "pairwise" (default, exact on one-for-ones)
                              or "additive" (cheaper, linear, and unsound
                              for mutual discovery over packages -- above).

    RETURNS one row per SIMULATED candidate, best-for-me first, with
    `my_delta` / `their_delta` and their paired standard errors and
    intervals, `mutual` (both sides significantly better off -- the trades
    that actually get voted through), the lineup and playoff-week
    diagnostics for both sides, and `screen_score` (the stage-1 `my_hat`,
    so a caller can see how badly the screen mis-ranked its own survivors).
    `result.attrs` records how many candidates were enumerated and how many
    were simulated.
    """
    if my_team not in rosters:
        raise KeyError(f"no roster for team {my_team!r}")
    if max_give < 1 or max_get < 1:
        raise ValueError("max_give and max_get must be at least 1")
    if screen_top is not None and screen_top < 1:
        raise ValueError("screen_top must be at least 1, or None to disable the screen")
    if screen_mode not in _SCREEN_MODES:
        raise ValueError(
            f"screen_mode {screen_mode!r} must be one of {sorted(_SCREEN_MODES)}"
        )

    teams = list(rosters)
    opponents = [team for team in teams if team != my_team] if their_teams is None \
        else list(their_teams)
    unknown = sorted(set(opponents) - set(teams))
    if unknown:
        raise KeyError(f"no roster for team(s) {unknown}")
    if my_team in opponents:
        raise ValueError(
            f"their_teams names {my_team!r}, which is my_team: a team cannot "
            "trade with itself"
        )

    draws = _SeasonDraws(
        teams, schedule, playoff_start_week=playoff_start_week, end_week=end_week,
        playoff_teams=playoff_teams, reseed=reseed, n=n, seed=seed,
    )
    prepared = _prepare(rosters, weekly, marginal, points_column, sd_column)
    mean_base, sd_base = _moments(prepared, teams, config, points_column, sd_column)
    baseline = draws.run(mean_base, sd_base)
    playoff_base = _playoff_win_prob(mean_base, sd_base)
    me = draws.index[my_team]

    enumerated = 0
    candidates: list[tuple[str, tuple[str, ...], tuple[str, ...], float]] = []
    for opponent in opponents:
        my_ids = list(prepared[my_team][id_column])
        their_ids = list(prepared[opponent][id_column])
        gives = _subsets(my_ids, max_give)
        gets = _subsets(their_ids, max_get)
        enumerated += len(gives) * len(gets)
        tables = _screen_tables(
            prepared, my_team, opponent, config, screen_mode,
            points_column, sd_column, id_column,
        )
        scored = []
        for give in gives:
            for get in gets:
                my_hat = _package_value(
                    give, get, tables.my_drop, tables.my_add, tables.my_pair
                )
                their_hat = _package_value(
                    get, give, tables.their_drop, tables.their_add, tables.their_pair
                )
                scored.append((opponent, give, get, my_hat, min(my_hat, their_hat)))
        candidates.extend(_screen(scored, screen_top))

    rows = []
    for opponent, give, get, screen_score in candidates:
        after_rosters = apply_trade(prepared, my_team, opponent, give, get, id_column=id_column)
        mean_after = mean_base.copy()
        sd_after = sd_base.copy()
        changed = [my_team, opponent]
        changed_mean, changed_sd = _moments(
            after_rosters, changed, config, points_column, sd_column
        )
        for offset, team in enumerate(changed):
            mean_after[draws.index[team]] = changed_mean[offset]
            sd_after[draws.index[team]] = changed_sd[offset]
        after = draws.run(mean_after, sd_after)
        playoff_after = _playoff_win_prob(mean_after, sd_after)

        them = draws.index[opponent]
        mine = _paired_delta(baseline.champion, after.champion, me, n)
        theirs = _paired_delta(baseline.champion, after.champion, them, n)
        rows.append({
            "their_team": opponent, "gives": give, "gets": get,
            "give_names": _names(rosters[my_team], give, id_column),
            "get_names": _names(rosters[opponent], get, id_column),
            "my_delta": mine.delta, "my_delta_se": mine.se,
            "my_delta_ci_low": mine.ci_low, "my_delta_ci_high": mine.ci_high,
            "my_significant": mine.significant,
            "their_delta": theirs.delta, "their_delta_se": theirs.se,
            "their_delta_ci_low": theirs.ci_low, "their_delta_ci_high": theirs.ci_high,
            "their_significant": theirs.significant,
            "mutual": bool(mine.ci_low > 0.0 and theirs.ci_low > 0.0),
            "my_exp_points_delta": float(mean_after[me] - mean_base[me]),
            "their_exp_points_delta": float(mean_after[them] - mean_base[them]),
            "my_playoff_win_prob_delta": float(playoff_after[me] - playoff_base[me]),
            "their_playoff_win_prob_delta": float(playoff_after[them] - playoff_base[them]),
            "my_championship_prob_before": float((baseline.champion == me).mean()),
            "my_championship_prob_after": float((after.champion == me).mean()),
            "their_championship_prob_before": float((baseline.champion == them).mean()),
            "their_championship_prob_after": float((after.champion == them).mean()),
            "screen_score": float(screen_score),
        })

    kept = _drop_dominated(rows) if drop_dominated else list(rows)
    result = pd.DataFrame(kept, columns=_FIND_COLUMNS)
    # Ties broken toward the SMALLER package: when two candidates move the
    # title identically, the one surrendering fewer players is strictly better
    # in reality even though this model cannot see the difference.
    if not result.empty:
        result = result.assign(_n_give=result["gives"].map(len)).sort_values(
            ["my_delta", "_n_give"], ascending=[False, True], kind="mergesort"
        ).drop(columns="_n_give")
    result = result.reset_index(drop=True)
    result.attrs.update({
        "n": n, "seed": seed,
        "monte_carlo_se": 0.5 / math.sqrt(n),
        "my_team": my_team, "their_teams": tuple(opponents),
        "max_give": max_give, "max_get": max_get,
        "screen_top": screen_top, "screen_mode": screen_mode,
        "candidates_enumerated": enumerated,
        "candidates_simulated": len(rows),
        "candidates_dominated": len(rows) - len(kept),
        "drop_dominated": drop_dominated,
    })
    return result


def _drop_dominated(rows: list[dict]) -> list[dict]:
    """Remove candidates that pay MORE for the SAME return.

    Bench depth is priced at exactly zero by this model: a player who never
    reaches the starting lineup in any simulated week contributes nothing, so
    adding him to the give side of a package leaves the championship delta
    bit-for-bit unchanged (common random numbers make the equality exact, not
    approximate). The visible consequence is that `find_trades` will happily
    surface `Jefferson + A.J. Brown -> Collins + Brown` beside the identical
    `Jefferson -> Collins + Brown`, i.e. advise throwing a real asset in for
    free. Advice that donates an asset for nothing is worse than no advice.

    A candidate is DOMINATED when, against the SAME counterparty, it receives
    exactly the same players while giving a strict SUPERSET, and gains nothing
    measurable for the extra. Only the minimal package survives.

    This does NOT fix the underlying zero-priced bench -- that needs the
    lineup re-optimised inside every simulated week, which is a `season.py`
    change and costs the runtime `season.py` exists to avoid. It stops a known
    pricing gap from being rendered as a recommendation.

    Dominance is about paying more for the same, NOT about package size: a
    bigger package that earns a better delta is a real trade and survives.
    """
    keep: list[dict] = []
    for row in rows:
        gives = frozenset(row["gives"])
        gets = frozenset(row["gets"])
        dominated = False
        for other in rows:
            if other is row:
                continue
            if other["their_team"] != row["their_team"]:
                continue  # a different team is a different trade, not a better one
            if frozenset(other["gets"]) != gets:
                continue
            other_gives = frozenset(other["gives"])
            if not other_gives < gives:
                continue  # not a strict subset -- nothing to compare
            # EXACT equality, not >=. Common random numbers make the two
            # deltas bit-identical precisely when the extra player never
            # reaches a starting lineup in any simulated world -- that exact
            # tie IS the free-sweetener signature. A merely-worse larger
            # package is a different trade with a real (if smaller) effect and
            # is left for the caller to judge, so this filter stays surgical
            # rather than pruning the candidate set on its own opinion.
            if (other["my_delta"] == row["my_delta"]
                    and other["their_delta"] == row["their_delta"]):
                dominated = True
                break
        if not dominated:
            keep.append(row)
    return keep


def _screen(
    scored: Sequence[tuple[str, tuple[str, ...], tuple[str, ...], float, float]],
    screen_top: int | None,
) -> list[tuple[str, tuple[str, ...], tuple[str, ...], float]]:
    """Stage 1's survivors: the top `screen_top` by "best for me" UNION the
    top `screen_top` by "best for the worse-off side", de-duplicated.

    Two keys, not one, because the two questions this module has to answer
    -- "what helps me most" and "what would they actually accept" -- have
    different answers, and ranking on either alone hides the other.
    Ties break on the enumeration order, which is deterministic, so the
    survivor set does not depend on sort stability beyond `sorted`'s own
    guarantee.
    """
    keep = [(opponent, give, get, my_hat) for opponent, give, get, my_hat, _ in scored]
    if screen_top is None:
        return keep
    best_for_me = sorted(range(len(scored)), key=lambda i: (-scored[i][3], i))[:screen_top]
    best_mutual = sorted(range(len(scored)), key=lambda i: (-scored[i][4], i))[:screen_top]
    chosen = sorted(set(best_for_me) | set(best_mutual))
    return [keep[i] for i in chosen]
