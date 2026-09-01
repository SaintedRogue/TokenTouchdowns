"""The mock draft simulator: MEASURE a draft strategy, don't assume it.

Every upstream module in this package (`projections`, `vor`, `survival`,
`draft`) answers "what should I do at THIS pick" -- none of them says
whether following that advice actually produces a better roster than the
alternative. This module runs a full simulated snake draft under a
candidate strategy and scores what it produced, so "is VOR-with-survival
actually better than pure ADP" becomes an experiment with a number attached
to it, not an assumption baked into how the board is sorted.

STRATEGY INTERFACE. A strategy is a plain callable:

    strategy(board: pd.DataFrame, roster: list[dict], pick: int,
             next_pick: int, teams: int) -> dict

`board` is the CURRENTLY AVAILABLE player pool (already excludes every
player drafted so far, by anyone); `roster` is the drafting team's own
picks so far (list of dicts, each at least `{"player_id", "position"}` --
exactly what `draft.roster_need`/`draft.recommend` already expect); `pick`
and `next_pick` are 1-indexed overall pick numbers for this team's current
and next turn (see `_next_pick_number` -- always defined, even on a team's
literal last pick of the draft, by extrapolating the snake pattern one
round further, so a strategy can always ask "would this survive to my next
turn" without a special-cased horizon). The return value is the chosen
player's row as a dict (i.e. `some_row.to_dict()`) -- `simulate_draft`
looks for `"player_id"` in it and nothing else, so a strategy is free to
carry extra columns through onto the roster it builds.

This is deliberately the SMALLEST interface that covers the three
strategies below, because Task 8 is expected to add more strategies
(e.g. one graded on real historical outcomes) without touching this
module. `strategy_adp` and `strategy_vor` need nothing else. `recommend`
(and therefore `strategy_vor_survival`) additionally needs a `LeagueConfig`
and doesn't fit this signature directly -- see `strategy_vor_survival`'s
own docstring for why it is a FACTORY (`strategy_vor_survival(config) ->
Strategy`) rather than a bare strategy.

ADP NOISE. `simulate_draft`'s only randomness is a single noise draw taken
once, up front, from `np.random.default_rng(seed)`:
`adp + Normal(0, adp_noise)` per player, stored as `_effective_adp`. This
represents ONE realized deviation of a real draft night from consensus
ADP (real drafters do not follow ADP with mathematical precision) -- it is
NOT re-drawn per pick, so the whole simulated draft is one coherent,
reproducible "world" for a given seed. Only `strategy_adp` reads
`_effective_adp`; `strategy_vor` and `strategy_vor_survival` rank by
`vor`/`expected_loss` and are unaffected by it -- a deliberate scoping
choice (noise models "how a pure-ADP drafter's board differs from the
model," not the market's underlying uncertainty, which `stdev` already
encodes inside `survival.add_survival`). `ADP_NOISE_DEFAULT` must be a
POSITIVE constant, not zero: with adp_noise=0, `strategy_adp` is a pure
function of `adp` with no randomness at all, and every trial in
`compare_strategies` would replay the identical draft -- silently making
the `trials` parameter meaningless for that strategy.

SCORING IS PLUGGABLE, ON PURPOSE (CRITICAL). `compare_strategies` takes a
`score_roster` callable and must NEVER be simplified to grade strategies on
this project's own `proj_points`/`vor` alone. `vor := proj_points -
replacement`, so a VOR-maximising strategy maximises "sum of proj_points of
the best lineup" BY CONSTRUCTION -- grading VOR against ADP with that
metric would beat ADP trivially and measure nothing but the fact that VOR
is defined as a subset of the score. The DEFAULT here (`optimal_lineup_score`,
proj_points of the optimal starting lineup) exists only to make this
module's own tests runnable without external data; it is exactly as
CIRCULAR as the paragraph above describes, and is not evidence that any
strategy is actually good. The parameter is what makes the real experiment
possible: Task 8 passes a `score_roster` built from ACTUAL historical
fantasy points for a completed season, which is not a function of this
project's own proj_points/vor at all, and is where a real answer comes
from.

THE ALL-ZERO SURVIVAL DEGENERATE CASE. A player absent from the ADP feed
gets `p_gone_by_next = 0` (see `survival.py`: "no adp at all" is modelled
as "certainly available forever"), so `expected_loss = vor * 0 = 0` for
every such player -- self-consistent early in a draft (nobody is drafting
them, so nothing can be lost), but in the late rounds of a deep league draft
EVERY remaining candidate can be in this state simultaneously (a real ADP
feed tracks a few hundred players; a deep league draft is more picks than
that), and `expected_loss` then carries no signal at all. See
`strategy_vor_survival`'s docstring for the documented fallback.
"""
from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

from .draft import FILLED_POSITION_DISCOUNT, recommend, roster_need
from .league import FLEX_ELIGIBLE, LeagueConfig, load_config_from_dict
from .survival import add_survival

# A strategy: given the currently available board, the drafting team's own
# roster so far, the pick happening now, that team's next pick, and the
# league's team count, return the chosen player's row as a dict. See the
# module docstring for the full reasoning behind this exact shape.
Strategy = Callable[[pd.DataFrame, list[dict], int, int, int], dict]

# Real FFC half-PPR ADP stdev (3,208 real drafts, checked 2026-08-31) ranges
# from ~2 near the top of the board to 15-20+ past pick 120 -- see task-7
# report for the full per-round breakdown. 6.0 sits in the middle of that
# range: enough to meaningfully reshuffle picks that are close together on
# the board (the case that matters -- nobody is confused about who goes
# 1st overall), without being so large it swamps a real 100+ pick ADP gap.
# Reasoned, not fit to any labelled outcome data -- same spirit as
# vor.py's TIER_GAP_MULTIPLIER and draft.py's FILLED_POSITION_DISCOUNT.
ADP_NOISE_DEFAULT = 6.0

# Standard rounds for a fantasy draft (1 QB/2 RB/2 WR/1 TE/1 FLEX/K/DEF is a
# 15-16 round convention); used only as compare_strategies' own default
# when a caller doesn't specify how deep to simulate.
DEFAULT_ROUNDS = 15

# Fallback league shape for `optimal_lineup_score` when no real
# `LeagueConfig` is available -- see that function's docstring. A generic
# 10-team, 3-way-flex half-PPR shape, deliberately NOT tied to any real
# league; real analysis should always pass its own config instead.
_FALLBACK_LEAGUE_CONFIG = load_config_from_dict({
    "leagueKey": "mock-default", "name": "Default 10-Team League",
    "numTeams": 10, "maxTeams": 10, "draftStatus": "predraft",
    "rosterSlots": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1},
    "scoring": [],
})

# Below this, an expected_loss value is treated as "no signal" rather than
# a genuine (if tiny) preference -- see strategy_vor_survival.
_EXPECTED_LOSS_EPSILON = 1e-9

# `strategy_vor_survival`'s fallback for "how many rounds are left" when its
# `rounds` factory argument is omitted -- see `_rounds_remaining`. Large
# enough that `draft.recommend`'s F11 need-urgency boost sits at its ~1.0
# floor regardless of `need`, i.e. "no information about the draft's total
# length" degrades to the pre-F11 behaviour rather than a false emergency.
_UNKNOWN_ROUNDS_REMAINING = 1_000


def _round_order(teams: int, round_index: int) -> list[int]:
    """Pick order (0-indexed team slots) within one round, 0-indexed
    `round_index`. Snake: even rounds run low-to-high, odd rounds high-to-low."""
    return list(range(teams)) if round_index % 2 == 0 else list(range(teams - 1, -1, -1))


def draft_order(teams: int, rounds: int) -> list[int]:
    """The full pick sequence for a snake draft: `teams * rounds` team
    slots (0-indexed), each round's order reversing from the last."""
    order: list[int] = []
    for round_index in range(rounds):
        order.extend(_round_order(teams, round_index))
    return order


def _pick_number(teams: int, round_index: int, slot: int) -> int:
    """1-indexed overall pick number for `slot`'s turn in round
    `round_index` (0-indexed). Well-defined for ANY round_index >= 0, even
    one beyond how many rounds a particular draft actually runs -- this is
    what lets `_next_pick_number` always answer "when does this team pick
    again," including on a team's true last pick of the draft, by treating
    the snake pattern as continuing one round further rather than as
    having a hard edge."""
    position_in_round = _round_order(teams, round_index).index(slot)
    return round_index * teams + position_in_round + 1


def _next_pick_number(teams: int, round_index: int, slot: int) -> int:
    """`slot`'s next pick after its turn in round `round_index`: always
    `round_index + 1`'s pick for that slot, which always exists (see
    `_pick_number`) and is always strictly after the current pick -- the
    minimum gap is 1 (the snake "turn," e.g. picks 4 then 5 for the last
    slot in a 4-team draft), so this can never violate
    `survival.add_survival`'s `next_pick > pick` requirement."""
    return _pick_number(teams, round_index + 1, slot)


def strategy_adp(board: pd.DataFrame, roster: list[dict], pick: int, next_pick: int, teams: int) -> dict:
    """Take the lowest-ADP player available. Reads `_effective_adp` (the
    noisy ranking `simulate_draft` builds once per draft -- see module
    docstring) when present, else the board's raw `adp` column, so this
    also works called directly outside a full simulation. A player with no
    ADP at all sorts last (pandas' default `na_position='last'` applies
    regardless of the noise draw, since `NaN + anything` stays `NaN`) --
    the honest behaviour for a strategy that only knows the consensus
    market: an unranked player is the last thing a pure-ADP drafter takes.
    """
    if board.empty:
        raise ValueError("strategy_adp: no players remain on the board")
    rank_column = "_effective_adp" if "_effective_adp" in board.columns else "adp"
    if rank_column not in board.columns:
        raise ValueError(f"strategy_adp requires a {rank_column!r} column on the board")
    chosen = board.sort_values(rank_column, na_position="last").iloc[0]
    return chosen.to_dict()


def strategy_vor(board: pd.DataFrame, roster: list[dict], pick: int, next_pick: int, teams: int) -> dict:
    """Take the highest-VOR player available -- the naive strategy
    `draft.py`'s module docstring argues against (it never asks whether
    that player would survive to your next pick anyway). Rows with no
    `vor` (positions this league doesn't start -- see `vor.add_vor`) are
    excluded, matching `draft.recommend`'s own filtering."""
    if board.empty:
        raise ValueError("strategy_vor: no players remain on the board")
    candidates = board[board["vor"].notna()]
    if candidates.empty:
        raise ValueError("strategy_vor: no player on the board has a defined vor")
    chosen = candidates.sort_values("vor", ascending=False).iloc[0]
    return chosen.to_dict()


def _need_discount(candidates: pd.DataFrame, roster: list[dict], config: LeagueConfig) -> pd.Series:
    """Same discount `draft.recommend` applies: 1.0 for a position whose
    starter need isn't met yet, `FILLED_POSITION_DISCOUNT` otherwise.
    Factored out so `strategy_vor_survival`'s all-zero fallback (below)
    can apply the identical roster-awareness to raw VOR that `recommend`
    would apply to expected_loss -- reusing `draft.py`'s own constant and
    need calculation rather than re-deriving a second version of it."""
    need = roster_need(roster, config)
    return candidates["position"].map(
        lambda position: 1.0 if need.get(position, 0) > 0 else FILLED_POSITION_DISCOUNT
    )


def _rounds_remaining(pick: int, teams: int, rounds: int | None) -> int:
    """How many rounds -- INCLUDING the one `pick` falls in -- are left in a
    `rounds`-round snake draft, for `draft.recommend`'s `rounds_remaining`
    (see F11 in fix-round-1-brief.md). `rounds` is None when the caller
    (`strategy_vor_survival`'s own factory argument) doesn't know the
    draft's total length; that degrades to `_UNKNOWN_ROUNDS_REMAINING`
    rather than guessing, the same "no information, no false alarm"
    reasoning `survival.py`'s own missing-adp convention uses."""
    if rounds is None:
        return _UNKNOWN_ROUNDS_REMAINING
    current_round = (pick - 1) // teams + 1
    return max(rounds - current_round + 1, 0)


def strategy_vor_survival(
    config: LeagueConfig, n: int = 5, conditional: bool = False, rounds: int | None = None,
) -> Strategy:
    """Build a strategy that ranks by `draft.recommend`'s expected-loss
    rule (`vor * P(gone before my next pick)`, roster-need discounted, and
    -- since F11 -- need-urgency boosted for a still-unfilled mandatory
    slot as the draft runs out of rounds).

    A FACTORY, not a bare strategy: `recommend` needs a `LeagueConfig` (for
    `roster_need`) that has nowhere to come from in the small, deliberately
    config-free `Strategy` signature every other strategy in this module
    uses (see module docstring) -- binding it here, once, at strategy-
    construction time keeps every actual per-pick call the same shape as
    `strategy_adp`/`strategy_vor`. `conditional` is passed straight through
    to `survival.add_survival` -- exposed here (default False, matching
    `add_survival`'s own default) purely so Task 8 can compare the
    conditional and unconditional survival forms under this same
    recommender by constructing two strategies, without editing this
    module (see `survival.py`'s own docstring for what the flag means).
    `rounds` (default None) is likewise bound at construction time and fed
    to `recommend`'s now-mandatory `rounds_remaining` via `_rounds_remaining`
    -- a caller that knows how deep this draft goes (i.e. whatever `rounds`
    it is about to pass to `simulate_draft`/`compare_strategies`) should
    pass the SAME value here so the need-urgency mechanism actually sees a
    real horizon instead of degrading to "no pressure."

    THE ALL-ZERO FALLBACK (see module docstring's "ALL-ZERO SURVIVAL
    DEGENERATE CASE"). `recommend` is asked for a full ranking (`n=` the
    entire candidate pool, not the caller's `n`) specifically so this
    function can inspect the TRUE best `expected_loss`, not a value that
    was already truncated to the top few rows before this check could see
    it: `recommend`'s own `.sort_values(...).head(n)` on an all-equal
    (all-zero) key is not guaranteed to include the actual highest-VOR
    player, so this function must never make its degenerate-case decision
    from an already-truncated frame.

    If the best remaining `expected_loss` exceeds `_EXPECTED_LOSS_EPSILON`,
    real signal exists and the top-ranked player is taken directly --
    ordinary `recommend` behaviour. Otherwise every candidate is
    ADP-invisible (or, in principle, every VOR is itself ~0 -- an
    edge case this same fallback also handles sensibly, not just the
    intended one) and `expected_loss` carries no information at all; the
    documented fallback is to rank by raw VOR instead, discounted by the
    SAME roster-need logic `recommend` would have applied (via
    `_need_discount`, reusing `draft.py`'s own `roster_need` and
    `FILLED_POSITION_DISCOUNT`) -- so a team that has already filled a
    position still prefers a genuine remaining need over one more player
    at a position it doesn't need, even once survival has nothing left to
    say. This was a deliberate choice among the alternatives the task
    brief raised: NOT board order (arbitrary, and silently determined by
    whatever order upstream code happened to produce), and NOT a fake ADP
    for unlisted players (would corrupt the survival model itself to patch
    a strategy-level ranking bug). Falling back to VOR is the one option
    that stays inside "we have no market signal for these players, so use
    the model's own opinion of how good they are" -- see
    `test_strategy_vor_survival_falls_back_to_raw_vor_when_every_candidate_has_zero_expected_loss`
    in test_mock.py, which fails under a naive implementation that just
    returns `recommend`'s (arbitrarily ordered) top row in this case.
    """

    def _pick(board: pd.DataFrame, roster: list[dict], pick: int, next_pick: int, teams: int) -> dict:
        if board.empty:
            raise ValueError("strategy_vor_survival: no players remain on the board")
        decorated = add_survival(board, pick, next_pick, conditional=conditional)
        rounds_remaining = _rounds_remaining(pick, teams, rounds)
        ranked = recommend(
            decorated, pick, next_pick, roster, config, teams, rounds_remaining,
            n=len(decorated) + 1,
        )
        if ranked.empty:
            raise ValueError("strategy_vor_survival: no draftable candidate (every vor is NaN)")

        best_loss = ranked["expected_loss"].iloc[0]
        if best_loss > _EXPECTED_LOSS_EPSILON:
            return ranked.iloc[0].to_dict()

        # Degenerate case: no candidate's expected_loss carries any signal.
        # Fall back to need-discounted raw VOR over the SAME candidate set
        # `recommend` already filtered (undrafted, vor not NaN).
        discount = _need_discount(ranked, roster, config)
        fallback_score = ranked["vor"] * discount
        chosen = ranked.loc[fallback_score.sort_values(ascending=False).index[0]]
        return chosen.to_dict()

    return _pick


def simulate_draft(
    board: pd.DataFrame,
    teams: int,
    rounds: int,
    my_slot: int,
    strategy: Strategy,
    seed: int,
    adp_noise: float = ADP_NOISE_DEFAULT,
    return_all: bool = False,
    opponent_strategy: Strategy | None = None,
) -> pd.DataFrame:
    """Run one full snake draft; return `my_slot`'s drafted roster (one row
    per round, in the order it was drafted) or, with `return_all=True`,
    every pick from every team (an extra `pick_number`, `round_number`,
    `slot` on each row -- namespaced away from a plausible real column like
    an NFL team abbreviation, which "team" would collide with).

    `my_slot` is 0-indexed, matching `draft_order`'s own team labels
    (0..teams-1); an out-of-range value raises immediately rather than
    silently returning an empty roster.

    By DEFAULT (`opponent_strategy=None`) all `teams` teams draft under the
    SAME `strategy`, with no separate "the market" vs. "my" policy. That
    asks the symmetric question "if the whole league drafted this way, how
    well would `my_slot`'s resulting roster score."

    `opponent_strategy`, when given, is used for every slot EXCEPT
    `my_slot`, which asks the other question: "how does MY policy do against
    a field that drafts some other way." `compare_strategies` needs this
    one, and not for realism alone -- under the symmetric default a
    deterministic strategy (`strategy_vor` reads only `vor`; nothing in it
    or in its nine identical opponents ever touches `_effective_adp`)
    reproduces a BYTE-IDENTICAL draft for every seed, so `trials` silently
    collapses to a single repeated sample. Task 7's real-data run measured
    exactly that: `std_score = 0.00` across 50 trials. Giving the opponents
    a market-following policy is what makes the per-trial seed reach the
    simulation at all, and therefore what makes a mock draft a simulation
    rather than a replay.

    Determinism: the input `board` is copied, never mutated (see
    `test_simulate_draft_does_not_mutate_the_input_board`). The only
    randomness anywhere in this function is a single noise draw from
    `np.random.default_rng(seed)` -- see module docstring's "ADP NOISE"
    section for exactly what it does and why it is drawn once, not per
    pick. `strategy` itself must be a deterministic function of its inputs
    for the whole draft to be reproducible; every strategy in this module
    is.
    """
    if not (0 <= my_slot < teams):
        raise ValueError(f"my_slot ({my_slot}) must be in range [0, {teams})")

    working = board.copy()
    if working["player_id"].duplicated().any():
        raise ValueError("simulate_draft: board contains duplicate player_id values")

    rng = np.random.default_rng(seed)
    # Drawn unconditionally (even when adp_noise == 0, where it is just an
    # array of zeros) so the RNG call sequence never depends on adp_noise's
    # value -- one predictable draw, always, which is what lets
    # test_adp_noise_produces_the_exact_hand_computed_order reproduce it
    # independently.
    noise = rng.normal(0.0, adp_noise, size=len(working))
    if "adp" in working.columns:
        working["_effective_adp"] = working["adp"].to_numpy() + noise

    available = working
    rosters: dict[int, list[dict]] = {slot: [] for slot in range(teams)}
    log: list[dict] = []

    order = draft_order(teams, rounds)
    for i, slot in enumerate(order):
        round_index = i // teams
        pick_number = i + 1
        next_pick_number = _next_pick_number(teams, round_index, slot)

        acting = strategy if (opponent_strategy is None or slot == my_slot) else opponent_strategy
        chosen = acting(available, rosters[slot], pick_number, next_pick_number, teams)
        chosen_id = chosen.get("player_id")
        if chosen_id is None or chosen_id not in set(available["player_id"]):
            raise ValueError(
                f"strategy returned a player_id ({chosen_id!r}) not currently on the "
                "board -- a strategy must choose from its `board` argument"
            )

        rosters[slot].append(chosen)
        available = available[available["player_id"] != chosen_id]
        if return_all:
            log.append({**chosen, "pick_number": pick_number, "round_number": round_index + 1, "slot": slot})

    if return_all:
        return pd.DataFrame(log)
    return pd.DataFrame(rosters[my_slot])


def optimal_lineup_score(config: LeagueConfig | None = None) -> Callable[[pd.DataFrame], float]:
    """Build a `score_roster` callable: sum of `proj_points` for the
    OPTIMAL starting lineup within a drafted roster.

    FIX (M-4, fix-round-2-brief.md): this used to route every roster slot --
    including flex -- through `league.starters_per_team`, which SPREADS a
    flex slot fractionally across its eligible positions (e.g. a single
    3-way RB/WR/TE flex becomes RB 2.333/WR 2.333/TE 1.333) and then
    `round()`ed each position independently. `round(2.333) == 2` for every
    one of RB/WR/TE, so the flex slot's own point of value -- "start a THIRD
    good RB/WR/TE, whichever is best" -- was rounded away entirely: a league
    with 9 real starting slots (1 QB, 2 RB, 2 WR, 1 TE, 1 FLEX, 1 K, 1 DEF)
    scored only 8, and no strategy ever got credit for stashing a strong
    flex-eligible player. This function now scores a genuine lineup instead:
    every NON-flex `config.roster_slots` entry (a plain position, not a key
    in `league.FLEX_ELIGIBLE`) is filled first, top-`count` by `proj_points`
    at that exact position; every remaining, not-yet-used player is then
    considered for each flex slot (in `roster_slots`' own iteration order),
    taking the top-`count` by `proj_points` among that slot's
    `FLEX_ELIGIBLE` positions -- "fill the fixed slots, then award the flex
    to the best remaining flex-eligible player," per the brief. This is a
    greedy two-pass lineup, not a full assignment-problem optimum across
    every slot type simultaneously; the real league (and every test fixture
    in this project) has exactly one flex slot type, for which greedy
    fixed-then-flex IS the optimum (a flex-eligible player can only ever
    improve on displacing the single flex slot's own current occupant, never
    a fixed slot that already took the best available player at its own
    position).

    THIS IS STILL THE CIRCULAR DEFAULT the module docstring's CRITICAL
    section warns about -- proj_points is exactly what `vor` is built from,
    so a VOR-maximising strategy scores well here BY CONSTRUCTION. It exists
    only so this module's own tests (and `compare_strategies`' bare
    `score_roster=None` default) work without external data. Real analysis
    must pass a `score_roster` built from something this project's own
    projections did not produce -- e.g. actual historical fantasy points.

    `config=None` falls back to a generic 10-team, 3-way-flex league shape
    (`_FALLBACK_LEAGUE_CONFIG`) -- needed because `compare_strategies`'
    own signature (task brief) has no `config` parameter of its own, so
    its bare, no-argument default call path (`compare_strategies(board,
    strategies, trials, teams, my_slot, seed)`, no `score_roster`, no
    `config`) must still produce SOME usable score rather than raising.
    Any real analysis should pass its own `config` explicitly instead of
    relying on this stand-in.
    """
    cfg = config if config is not None else _FALLBACK_LEAGUE_CONFIG
    fixed_targets: dict[str, int] = {}
    flex_slots: list[tuple[str, tuple[str, ...], int]] = []
    for slot, count in cfg.roster_slots.items():
        eligible = FLEX_ELIGIBLE.get(slot)
        if eligible:
            flex_slots.append((slot, eligible, int(count)))
        else:
            fixed_targets[slot] = fixed_targets.get(slot, 0) + int(count)

    def _score(roster: pd.DataFrame) -> float:
        if roster.empty:
            return 0.0
        total = 0.0
        used_index: set = set()
        for position, target in fixed_targets.items():
            if target <= 0:
                continue
            pool = roster[roster["position"] == position].sort_values("proj_points", ascending=False)
            top = pool.head(target)
            total += float(top["proj_points"].sum())
            used_index.update(top.index)
        remaining = roster.drop(index=used_index, errors="ignore")
        for _slot, eligible, target in flex_slots:
            if target <= 0:
                continue
            pool = remaining[remaining["position"].isin(eligible)].sort_values(
                "proj_points", ascending=False
            )
            top = pool.head(target)
            total += float(top["proj_points"].sum())
            used_index.update(top.index)
            remaining = remaining.drop(index=top.index)
        return total

    return _score


def compare_strategies(
    board: pd.DataFrame,
    strategies: dict[str, Strategy],
    trials: int,
    teams: int,
    my_slot: int,
    seed: int,
    rounds: int = DEFAULT_ROUNDS,
    adp_noise: float = ADP_NOISE_DEFAULT,
    score_roster: Callable[[pd.DataFrame], float] | None = None,
    config: LeagueConfig | None = None,
    opponent_strategy: Strategy | None = None,
) -> pd.DataFrame:
    """Run `trials` simulated drafts per strategy and score each resulting
    `my_slot` roster, returning one summary row per strategy: `strategy`,
    `trials`, `mean_score`, `std_score`, `sem_score`, `ci95_low`,
    `ci95_high`, `min_score`, `max_score`.

    TRIALS MUST ACTUALLY VARY (read before changing `opponent_strategy`).
    The strategy under test is evaluated against a FIELD of opponents, which
    defaults to `strategy_adp` -- the noisy consensus market. This is not
    only the more realistic question; it is what makes `trials` mean
    anything. `strategy_vor` and `strategy_vor_survival` are deterministic
    functions of the board, so if the opposing teams also ignored the
    per-trial ADP noise draw (which they did, under the old leaguewide-
    symmetric default) every trial replayed the identical draft. Task 7
    measured `std_score = 0.00` over 50 trials for both, meaning its
    headline comparison rested on three single samples with no way to
    separate a real difference from noise. Passing `opponent_strategy` a
    strategy that ignores `_effective_adp` reintroduces exactly that defect;
    pass `opponent_strategy=None` only when deliberately asking the
    symmetric "if the whole league drafted this way" question, and expect
    zero variance from a deterministic strategy when you do.

    UNCERTAINTY IS REPORTED, NOT LEFT TO THE READER. `sem_score` is the
    standard error of `mean_score` (the sample standard deviation, ddof=1,
    over sqrt(trials)) and `ci95_low`/`ci95_high` are the normal-
    approximation 95% interval around the mean. A difference between two
    strategies whose intervals overlap is not a result, and reporting the
    mean alone is precisely how Task 7's unpowered table came to look like
    one. With `trials == 1` there is no spread to estimate, so `sem_score`
    and the interval bounds are NaN rather than a falsely precise 0.

    SCORING IS PLUGGABLE -- read this before changing `score_roster`'s
    default. `score_roster` defaults to `optimal_lineup_score(config)`,
    which grades a roster on THIS PROJECT'S OWN `proj_points`. That default
    is CIRCULAR for comparing strategies derived from the same projections
    (VOR is defined as `proj_points - replacement`, so a VOR-based strategy
    maximises the default grading function by construction and would beat
    ADP trivially, proving nothing). The default exists ONLY so this
    module's own tests, and any quick sanity run, work with zero external
    data. A REAL comparison between strategies must pass a `score_roster`
    built from data this project's own projections did not produce --
    e.g. actual historical fantasy points for a completed season (see
    Task 8). Do not remove or quietly bypass this parameter; it is the
    entire reason a strategy comparison here can mean anything.

    COMMON RANDOM NUMBERS. All strategies in one `compare_strategies` call
    share the exact same sequence of `trials` per-trial seeds, drawn once
    from `np.random.default_rng(seed)`. So trial `i` is the same
    randomized "world" (same ADP noise draw) for every strategy being
    compared -- any score difference between strategies on trial `i` is
    attributable to the strategies' decisions, not to different strategies
    happening to see different random worlds. This is a standard variance-
    reduction technique (paired comparison / common random numbers), and
    is why two strategy names bound to the literal same callable score
    IDENTICALLY across trials (see
    test_compare_strategies_uses_common_random_numbers_across_strategies).

    `rounds` and `adp_noise` are passed straight through to every
    `simulate_draft` call (not part of the brief's own listed signature,
    which had no way to reach `rounds` at all -- `simulate_draft` requires
    it -- so both are added here as optional keyword arguments with
    defaults matching `simulate_draft`'s own).
    """
    scorer = score_roster if score_roster is not None else optimal_lineup_score(config)
    # `strategy_adp` is the default FIELD, not a default for the strategy
    # under test -- see this function's docstring for why an opponent model
    # that responds to the per-trial noise is what makes `trials` real.
    opponents = strategy_adp if opponent_strategy is None else opponent_strategy
    trial_seeds = np.random.default_rng(seed).integers(0, 2**31 - 1, size=trials).tolist()

    rows = []
    for name, strategy in strategies.items():
        scores = [
            scorer(simulate_draft(
                board, teams, rounds, my_slot, strategy, seed=trial_seed,
                adp_noise=adp_noise, opponent_strategy=opponents,
            ))
            for trial_seed in trial_seeds
        ]
        # ddof=1: these trials are a SAMPLE of possible draft nights, not the
        # population of them, and the standard error of the mean is what the
        # reader needs to judge whether a gap between two strategies is real.
        sem = float(np.std(scores, ddof=1) / np.sqrt(trials)) if trials > 1 else float("nan")
        mean = float(np.mean(scores))
        rows.append({
            "strategy": name,
            "trials": trials,
            "mean_score": mean,
            "std_score": float(np.std(scores)),
            "sem_score": sem,
            "ci95_low": mean - 1.96 * sem,
            "ci95_high": mean + 1.96 * sem,
            "min_score": float(np.min(scores)),
            "max_score": float(np.max(scores)),
        })
    return pd.DataFrame(rows)
