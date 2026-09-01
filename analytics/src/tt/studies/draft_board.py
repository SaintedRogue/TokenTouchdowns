"""Task 8: the honest, out-of-sample draft-strategy backtest.

Every prior strategy comparison in this project graded a mock draft's
rosters on THIS PROJECT'S OWN `proj_points` -- which is circular for VOR
(`vor := proj_points - replacement`), so a VOR-driven strategy maximises
that grading function by construction and "wins" whether or not it is
actually good. This module replaces that broken instrument with a real one:

  1. Fit `projections.project_players` using ONLY seasons STRICTLY BEFORE
     the backtest season S (see `build_projection_board`'s `history["season"]
     < season` filter -- non-negotiable, the exact lookahead-bias error
     class already caught twice in this repo).
  2. Draft against season S's OWN preseason ADP (`load_ffc_crosswalk`),
     never the current board.
  3. Grade each strategy's roster on the OPTIMAL STARTING LINEUP's REAL,
     ACTUAL fantasy points from season S -- computed from nflverse's own
     per-week stats via THIS league's `scoring_weights`, `season_type ==
     'REG'` only (`projections.regular_season`) -- never `proj_points`.
     A drafted player with no stats that season scores ZERO, not NaN and
     not dropped (`actual_points_by_player`'s groupby simply never produces
     a row for a player with zero qualifying weeks, and `actual_lineup_
     score`'s `.map(...).fillna(0.0)` is what turns "absent from the
     Series" into an explicit zero rather than a silently-excluded pick).

THE CROSSWALK (why this exists at all). Task 7's real-data run joined FFC
ADP to nflverse by exact name and matched only ~16% of the board -- which
starved `survival.add_survival`'s signal on the large majority of picks and
made that run's "VOR-with-survival loses" result unable to distinguish a
real finding from a broken join. `parse_ffc_crosswalk`/`load_ffc_crosswalk`
instead consume the output of `analytics/scripts/build_ffc_crosswalk.mjs`,
which resolves FFC's `name`+`position`+`team` to nflverse's own `player_id`
via the project's TESTED fuzzy matcher (`src/identity.js`'s
`buildAdpIndex`/`matchAdp` -- reused, not reimplemented in Python) run
twice: first against Sleeper's `gsis_id` (which IS nflverse's `player_id`,
per the task brief), then, for anything Sleeper leaves unresolved, directly
against nflverse's own player roster names. The second pass exists because
Sleeper's `gsis_id` field turned out to have real, material gaps for
exactly the players a draft-strategy backtest cares about most -- checked
live against Sleeper's own API 2026-09-01: Ja'Marr Chase, Bijan Robinson,
Amon-Ra St. Brown, Puka Nacua, Garrett Wilson and Jahmyr Gibbs all carry
`gsis_id: null` there. See the Node script's own module docstring for the
full account; the result is a 91-93% overall match rate and a ~99%
match rate restricted to QB/RB/WR/TE (the only positions this pipeline ever
drafts -- see `projections.PROJECTABLE_POSITIONS`), against the prior ~16%.

WHY VOR IS RECOMPUTED PER TEAM COUNT, NOT ONCE. `vor.add_vor`'s replacement
level is a direct function of `teams` (see that module's own docstring), so
`build_board` -- unlike `build_projection_board`, which is teams-independent
-- takes `teams` and calls `add_vor` itself; comparing team counts 4/6/8/10
means calling it four separate times per season, not slicing one board four
ways.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path

import numpy as np
import pandas as pd

from ..league import LeagueConfig, scoring_weights
from ..mock import (
    DEFAULT_ROUNDS,
    Strategy,
    compare_strategies,
    optimal_lineup_score,
    simulate_draft,
    strategy_adp,
    strategy_vor,
    strategy_vor_survival,
)
from ..projections import project_players, regular_season
from ..scoring import score_frame
from ..vor import add_vor

# Backtest seasons this study covers -- the three completed NFL seasons with
# full nflverse history available as of this task (see progress.md's
# controller-verified data inventory: 2023/2024/2025 each carry weeks 1-22,
# REG+POST). NOT a caller default baked into every function below (each
# function takes its own `season`/`seasons`) -- exposed here purely as the
# canonical list the real backtest run and its report both reference.
BACKTEST_SEASONS: tuple[int, ...] = (2023, 2024, 2025)

# Team counts to compare -- the real league (4 teams joined, 10-team
# maximum, see analytics/data/league.json) spans this whole range.
TEAM_COUNTS: tuple[int, ...] = (4, 6, 8, 10)

_FFC_CROSSWALK_COLUMNS = (
    "player_id", "name", "position", "team", "adp", "stdev", "high", "low",
    "times_drafted", "match_state", "match_source",
)

# Column rename from analytics/scripts/build_ffc_crosswalk.mjs's JSON output
# (camelCase, matching src/sources/ffc.js's own normalize() shape) to this
# module's snake_case.
_FFC_JSON_RENAME = {
    "playerId": "player_id",
    "timesDrafted": "times_drafted",
    "matchState": "match_state",
    "matchSource": "match_source",
}


def parse_ffc_crosswalk(payload: dict) -> pd.DataFrame:
    """Parse one season's `{season, meta, players}` crosswalk payload (the
    JSON shape `build_ffc_crosswalk.mjs` writes) into a DataFrame.

    A pure function of an already-loaded dict -- not of a file path -- so
    tests can exercise it without touching disk (mirrors `tt.league`'s own
    `load_config_from_dict`/`load_config` split). `player_id` is nflverse's
    id (sourced from the crosswalk script's Sleeper-then-nflverse-name
    resolution); a player the crosswalk could not resolve (`match_state`
    `'ambiguous'` or `'absent'`) carries `player_id = None`, which pandas
    reads as NaN once the column is built -- `attach_adp` relies on that to
    exclude unresolved rows from the join rather than merging on a literal
    `None` key.
    """
    players = payload.get("players") or []
    if not players:
        return pd.DataFrame(columns=list(_FFC_CROSSWALK_COLUMNS))
    out = pd.DataFrame(players).rename(columns=_FFC_JSON_RENAME)
    for column in _FFC_CROSSWALK_COLUMNS:
        if column not in out.columns:
            out[column] = pd.NA
    return out[list(_FFC_CROSSWALK_COLUMNS)]


def load_ffc_crosswalk(path: str | Path) -> pd.DataFrame:
    """`parse_ffc_crosswalk` from a JSON file on disk -- the file
    `build_ffc_crosswalk.mjs` writes to `analytics/data/ffc_adp_<season>.json`
    (gitignored; the Node script must be re-run to produce it -- it is not
    checked in, per this project's "never commit data files" rule)."""
    payload = json.loads(Path(path).read_text())
    return parse_ffc_crosswalk(payload)


def attach_adp(board: pd.DataFrame, ffc: pd.DataFrame) -> pd.DataFrame:
    """Left-join `adp`/`stdev` from `ffc` onto `board` by `player_id`.

    Only crosswalk-resolved rows (`player_id` not null) are ever joined --
    an unresolved FFC row has no `player_id` to join on at all, and merging
    on a null key would either match nothing (safe but wasteful) or, worse,
    collide every unresolved row with every board row that also lacked a
    key, which cannot happen here (`board`'s `player_id` always comes from
    nflverse and is never null) but is exactly the kind of silent footgun
    this project's join code (see `src/identity.js`'s own "never guess"
    design) is built to avoid.

    A `player_id` appearing more than once in the resolved crosswalk would
    mean two different FFC rows independently resolved to the SAME
    nflverse player -- genuinely ambiguous which ADP to attach -- and raises
    rather than silently picking one (defence in depth: the crosswalk script
    already checked for this and found none in the real data, see task-8
    report).

    Board rows with no crosswalk match keep `adp`/`stdev` as NaN --
    `survival.add_survival`'s own documented convention for "no ADP data at
    all: certainly available forever" then applies unchanged.
    """
    matched = ffc[ffc["player_id"].notna()]
    dupes = matched.loc[matched["player_id"].duplicated(keep=False), "player_id"].unique()
    if len(dupes):
        raise ValueError(
            f"attach_adp: ffc crosswalk resolves multiple rows to the same "
            f"player_id {sorted(dupes)} -- ambiguous which ADP to attach"
        )
    return board.merge(matched[["player_id", "adp", "stdev"]], on="player_id", how="left")


def build_projection_board(
    history: pd.DataFrame,
    config: LeagueConfig,
    season: int,
    ffc: pd.DataFrame,
    seed: int | None = None,
) -> pd.DataFrame:
    """Project + attach ADP for one backtest season, WITHOUT VOR (see module
    docstring's "WHY VOR IS RECOMPUTED PER TEAM COUNT" -- that step needs a
    `teams` this function deliberately doesn't take).

    THE NO-LOOKAHEAD GUARANTEE LIVES HERE. `project_players` is called with
    `seasons` = every season in `history` STRICTLY BEFORE `season` -- never
    `season` itself and never anything after it. This is the one piece of
    this whole module that, if it silently regressed (e.g. someone "fixed"
    this to include `season` because it "gives a better fit"), would
    invalidate the entire backtest by construction: drafting on a
    projection that has already seen the answer is not a backtest, it's a
    circular check with extra steps. `test_build_projection_board_never_
    trains_on_the_backtest_season_itself` pins this directly by giving the
    target season a wildly different volume than history and asserting the
    projection is UNCHANGED by it.

    Raises if `history` has no season strictly before `season` at all --
    silently projecting from nothing (or from `season` itself, the only
    other option) would be worse than failing loudly.
    """
    seasons_present = {int(s) for s in history["season"].unique()}
    train_seasons = tuple(sorted(s for s in seasons_present if s < season))
    if not train_seasons:
        raise ValueError(
            f"build_projection_board: no seasons strictly before {season} in "
            "history -- cannot draft it without lookahead bias"
        )
    projections = project_players(history, config, seasons=train_seasons, seed=seed)
    return attach_adp(projections, ffc)


def build_board(
    history: pd.DataFrame,
    config: LeagueConfig,
    season: int,
    ffc: pd.DataFrame,
    teams: int,
    seed: int | None = None,
) -> pd.DataFrame:
    """`build_projection_board` plus `vor.add_vor` for a specific `teams`
    count -- the full board `mock.compare_strategies` needs (proj_points,
    vor, tier, adp, stdev)."""
    board = build_projection_board(history, config, season, ffc, seed=seed)
    return add_vor(board, config, teams=teams)


def actual_points_by_player(
    history: pd.DataFrame, config: LeagueConfig, season: int,
) -> pd.Series:
    """Real fantasy points actually scored in `season`, per player, using
    THIS league's own `scoring_weights` -- never `proj_points`, never
    nflverse's own `fantasy_points`/`fantasy_points_ppr` columns (neither is
    this league's scoring; see `tt.scoring`'s module docstring for the same
    reasoning applied to weekly rows).

    REG season only (`projections.regular_season`): a player's playoff-run
    stats are a different population from what a fantasy league scores
    (see `projections.REGULAR_SEASON_TYPE`'s own reasoning -- the same
    contamination risk applies here as it did to training data).

    Stat totals are summed across `season`'s REG weeks per player FIRST,
    then scored once (`scoring.score_frame`) -- equivalent to scoring each
    week and summing (the scoring formula is linear in every stat), just
    one call instead of N.

    Returns a Series indexed by `player_id`; a player who played zero
    qualifying (REG, `season`) weeks has NO entry here at all -- by design,
    not an oversight. `actual_lineup_score` is what turns "absent from this
    Series" into an explicit, scored ZERO for a drafted bust; this function
    itself simply never fabricates a row nflverse gave no data for.
    """
    weights = scoring_weights(config)
    reg = regular_season(history)
    season_rows = reg[reg["season"] == season]
    if season_rows.empty:
        return pd.Series(dtype=float, name="actual_points")
    missing = [column for column in weights if column not in season_rows.columns]
    if missing:
        raise ValueError(
            f"actual_points_by_player: history is missing column(s) {missing} "
            "that this league's scoring_weights requires"
        )
    totals = season_rows.groupby("player_id")[list(weights.keys())].sum()
    points = score_frame(totals, weights=weights)
    points.name = "actual_points"
    return points


def actual_lineup_score(
    actual_points: pd.Series, config: LeagueConfig,
) -> Callable[[pd.DataFrame], float]:
    """Build a `score_roster` callable (the interface `mock.compare_
    strategies` accepts) that grades a drafted roster on REAL, ACTUAL
    fantasy points rather than this project's own `proj_points` -- the
    entire point of this module (see its docstring's CIRCULARITY warning).

    Reuses `mock.optimal_lineup_score` -- "top `round(starters_per_team[pos])`
    players by `proj_points`, summed; bench players score nothing" -- for the
    OPTIMAL-LINEUP-ONLY rule itself (identical logic, already tested), by
    handing it a roster whose `proj_points` column has been REPLACED with
    real `actual_points` first. This is not a trick to avoid writing new
    code; it is the literal contract Task 8's brief asks for ("score the
    optimal starting lineup per starters_per_team") applied to a different
    points source, so reusing the exact tested rule is more trustworthy than
    a parallel reimplementation that could quietly diverge from it (e.g. a
    different rounding rule for the starter target).

    A drafted player absent from `actual_points` (see that function's
    docstring -- zero qualifying weeks that season, a bust or an injury)
    maps to NaN via `.map`, then to an explicit `0.0` via `.fillna` -- the
    brief's "a bust scores ZERO, not NaN and not excluded" requirement,
    satisfied at exactly this line.
    """
    base_score = optimal_lineup_score(config)

    def _score(roster: pd.DataFrame) -> float:
        if roster.empty or "player_id" not in roster.columns:
            return base_score(roster)
        decorated = roster.assign(
            proj_points=roster["player_id"].map(actual_points).fillna(0.0)
        )
        return base_score(decorated)

    return _score


def zero_scoring_rate(roster: pd.DataFrame, actual_points: pd.Series) -> tuple[int, int]:
    """`(count of roster picks that scored zero actual points, roster size)`
    for one drafted roster -- NOT restricted to starters (unlike `actual_
    lineup_score`): the brief asks how many DRAFTED players scored zero,
    which includes busts stashed on the bench, not just ones that would
    have started."""
    if roster.empty or "player_id" not in roster.columns:
        return (0, 0)
    scored = roster["player_id"].map(actual_points).fillna(0.0)
    return int((scored == 0.0).sum()), int(len(scored))


def strategies_for(config: LeagueConfig, rounds: int) -> dict[str, Strategy]:
    """The four arms Task 8 compares: pure ADP, pure VOR, and VOR-with-
    survival under BOTH the unconditioned and conditioned forms of
    `survival.add_survival` (see that module's own docstring for what the
    `conditional` flag means -- this is the empirical comparison Task 6
    deliberately left unresolved for Task 8 to settle with data).

    A single shared `rounds` (matching whatever `compare_strategies` will
    actually run) is threaded to both `strategy_vor_survival` factories so
    `draft.recommend`'s F11 need-urgency mechanism sees a real horizon --
    see `mock.strategy_vor_survival`'s own docstring on why a caller that
    knows the draft depth should always pass it here.
    """
    return {
        "adp": strategy_adp,
        "vor": strategy_vor,
        "vor_survival_unconditional": strategy_vor_survival(config, rounds=rounds, conditional=False),
        "vor_survival_conditional": strategy_vor_survival(config, rounds=rounds, conditional=True),
    }


def run_backtest_cell(
    history: pd.DataFrame,
    config: LeagueConfig,
    season: int,
    ffc: pd.DataFrame,
    teams: int,
    my_slot: int,
    trials: int,
    seed: int,
    rounds: int = DEFAULT_ROUNDS,
) -> pd.DataFrame:
    """One (season, teams) cell of the backtest: build that season's board
    at that team count, score every strategy in `strategies_for` on ACTUAL
    season-`season` points, and return `compare_strategies`' summary table
    with `season`/`teams` columns prepended.
    """
    board = build_board(history, config, season, ffc, teams)
    actual = actual_points_by_player(history, config, season)
    scorer = actual_lineup_score(actual, config)
    strategies = strategies_for(config, rounds)
    result = compare_strategies(
        board, strategies, trials=trials, teams=teams, my_slot=my_slot,
        seed=seed, rounds=rounds, score_roster=scorer, config=config,
    )
    result.insert(0, "season", season)
    result.insert(1, "teams", teams)
    return result


def zero_scoring_diagnostics(
    history: pd.DataFrame,
    config: LeagueConfig,
    season: int,
    ffc: pd.DataFrame,
    teams: int,
    my_slot: int,
    seed: int,
    rounds: int = DEFAULT_ROUNDS,
) -> pd.DataFrame:
    """One deterministic draft per strategy (`compare_strategies`' own first
    trial seed for this `seed`, reproduced independently via the identical
    `np.random.default_rng(seed).integers(...)` call it uses -- see `mock.
    compare_strategies`' "COMMON RANDOM NUMBERS" docstring section), reporting
    how many of that strategy's `my_slot` picks scored zero actual points.

    A diagnostic, not a formal per-arm statistic (one draft, not `trials`
    of them) -- exists purely to answer the brief's "state how many drafted
    players scored zero per arm" requirement without re-simulating the
    entire `trials`-sized run just to inspect individual rosters (`compare_
    strategies` returns only aggregate scores, by design).
    """
    board = build_board(history, config, season, ffc, teams)
    actual = actual_points_by_player(history, config, season)
    strategies = strategies_for(config, rounds)
    trial_seed = int(np.random.default_rng(seed).integers(0, 2**31 - 1, size=1)[0])

    rows = []
    for name, strategy in strategies.items():
        roster = simulate_draft(
            board, teams, rounds, my_slot, strategy, seed=trial_seed,
            opponent_strategy=strategy_adp,
        )
        zero, total = zero_scoring_rate(roster, actual)
        rows.append({
            "strategy": name, "picks": total, "zero_scoring": zero,
            "zero_rate": zero / total if total else float("nan"),
        })
    return pd.DataFrame(rows)


def run_backtest(
    history: pd.DataFrame,
    config: LeagueConfig,
    seasons: Iterable[int],
    ffc_by_season: dict[int, pd.DataFrame],
    team_counts: tuple[int, ...] = TEAM_COUNTS,
    trials: int = 200,
    seed: int = 2026,
    rounds: int = DEFAULT_ROUNDS,
    my_slot_for_teams: Callable[[int], int] | None = None,
) -> pd.DataFrame:
    """The full backtest: every (season, teams) cell in `seasons` x
    `team_counts`, concatenated into one table.

    `my_slot_for_teams` (default `teams // 2`, a fixed, non-tuned "middle of
    the draft order" choice -- not favouring the earliest or latest pick,
    and not chosen by trying values until some strategy won) maps a team
    count to which 0-indexed slot this study drafts from; a caller who wants
    a different rule (e.g. always slot 0) can override it.

    The SAME `seed` is reused across every cell -- deliberate, not an
    oversight: each cell's board differs by season and by `teams` already
    (different players, different replacement levels), so reusing the seed
    does not replay identical drafts across cells, and doing so keeps the
    per-trial ADP-noise draws comparable across cells (common random
    numbers, extended across the whole study, not just within one
    `compare_strategies` call).
    """
    slot_for = my_slot_for_teams if my_slot_for_teams is not None else (lambda teams: teams // 2)
    rows = []
    for season in seasons:
        ffc = ffc_by_season[season]
        for teams in team_counts:
            rows.append(run_backtest_cell(
                history, config, season, ffc, teams, slot_for(teams),
                trials, seed, rounds=rounds,
            ))
    return pd.concat(rows, ignore_index=True)
