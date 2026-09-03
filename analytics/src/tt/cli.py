"""Phase 2 CLI adapter (docs/draft-engine-design.md section 4).

`tt draft board`/`pick`/`mock`/`lineup`/`playoff`/`season`/`trade` live on
ONE Node CLI surface (`bin/tt.js` -> `src/cli.js`), but the analytics
engine is Python.
This module is the Python half of the bridge: `src/analytics.js` spawns
`analytics/.venv/bin/python -m tt.cli <subcommand> ...`, writes a JSON
payload to its stdin, and parses exactly one JSON document back off stdout.

THIS FILE IS A THIN ADAPTER, ON PURPOSE. Every subcommand function below
does the same three things -- parse inputs (league config + nflverse
parquet from disk, a flags Namespace, a JSON object read from stdin),
call an EXISTING `tt.*` module, serialise the result -- and nothing else.
No projection, VOR, survival, draft, mock, lineup or playoff LOGIC is
implemented here; see `tt.projections`, `tt.vor`, `tt.survival`,
`tt.draft`, `tt.mock`, `tt.lineup`, `tt.playoff` for that. The one piece of
arithmetic this module does own is translating `--slot`/`--round` into
1-indexed overall pick numbers (`_pick_number`/`_next_pick_number`, both
reused directly from `tt.mock` rather than re-derived -- see the imports
below) -- that is CLI-level bookkeeping ("when do I pick next"), not a
modelling decision.

STDOUT DISCIPLINE (why this matters). `main` builds the ENTIRE JSON result
in memory and writes it to stdout in exactly one `print()` call, only after
every step has already succeeded -- never incrementally, never before
error handling has had a chance to run. This is deliberate: `projections.
project_players` can legitimately emit a `UserWarning` (unmodellable
scoring, e.g. a league that scores Ret TD or 2-PT -- see that module's
`_warn_about_unmodellable_scoring`), and Python's default warning handler
writes to stderr, not stdout -- but if this module ever grew a stray
`print()` for progress/debugging, THAT would land on stdout and corrupt the
one JSON document `src/analytics.js` expects to parse. There is exactly one
`print(...)` in this whole module (in `main`, on the success path) for
precisely this reason.

INPUT CHANNELS.
  - League config + nflverse weekly parquet are read directly from disk
    (`--config`, default `data/league.json`; `--data-dir`, default `data`)
    -- both are already local files this project's own tooling produces
    (`tt league export`, `tt sync`'s nflverse ingest), and neither is
    remotely JSON-small enough to usefully pipe through stdin.
  - Everything Node fetches fresh per invocation (a live Yahoo roster for
    `lineup`/`playoff`) arrives as a JSON object on stdin: `{"roster": [...]}`
    for `lineup`, `{"roster": [...], "opponent_roster": [...]}` for
    `playoff`. `pick`'s in-progress-draft roster is also read from stdin
    (`{"roster": [...]}`) -- Node has no live source for "what have I
    drafted so far tonight" (that is not something the Yahoo roster
    endpoint tracks), so the CLI layer reads it from a user-maintained JSON
    file and forwards its contents unchanged.
  - `board` and `mock` need no stdin payload at all (an empty object is
    fine) -- both are pure functions of the league config, the historical
    data, and CLI flags.

IDENTITY / THE YAHOO<->NFLVERSE JOIN. `lineup`/`playoff` roster entries
carry a `player_id` that Node has ALREADY resolved to nflverse's own id
(via `src/identity.js`'s tested crosswalk, joined through the cached
Sleeper payload's `gsis_id`) -- this module does no name-matching of its
own, deliberately (an earlier ad-hoc exact-name join elsewhere in this
project matched only 16% of players; the tested crosswalk matches
91-93%). A roster entry Node could not resolve arrives with
`player_id: null`; this module never drops it -- it is carried through as
an unprojectable player (visible in the lineup as a named, pointless-total
bench/empty-slot row) and named again in this module's own
`unprojected_players` list, which also catches a RESOLVED id that simply
has no projection (e.g. a K/DEF -- this engine only projects QB/RB/WR/TE).
`src/cli.js` combines this list with Node's own crosswalk match-rate
footer so a user sees both "how many resolved" and "who specifically
didn't."
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any

import pandas as pd

from .draft import recommend
from .league import LeagueConfig, load_config
from .lineup import optimal_lineup
from .mock import (
    DEFAULT_ROUNDS,
    ADP_NOISE_DEFAULT,
    Strategy,
    _pick_number,  # noqa: F401 -- deliberately reused, not re-derived; see module docstring
    compare_strategies,
    simulate_draft,
    strategy_adp,
    strategy_vor,
    strategy_vor_survival,
)
from .playoff import DEFAULT_N as PLAYOFF_DEFAULT_N, playoff_lineup
from .projections import project_players
from .season import DEFAULT_N as SEASON_DEFAULT_N, round_robin_schedule, simulate_season
from .studies.draft_board import attach_adp
from .survival import add_survival
from .trade import DEFAULT_N as TRADE_DEFAULT_N, evaluate_trade, find_trades
from .vor import add_vor
from .weekly import project_week

DEFAULT_MC_N = 5000
DEFAULT_TRIALS = 50
DEFAULT_BOARD_COUNT = 50
DEFAULT_PICK_N = 5
STRATEGY_NAMES = ("adp", "vor", "vor_survival")

# `tt season`'s CLI-level fallback for --playoff-start-week/--end-week when
# neither is given -- THIS PROJECT'S OWN live league settings (verified
# 2026-09-01: playoff_start_week 16, end_week 17), used ONLY as a
# convenience default for the --mock-draft / --rosters=PATH DEMONSTRATION
# path (see cmd_season). This is CLI ergonomics, not analytics: the same
# category as DEFAULT_PICK_N above or cmd_mock's `seed = ... else 2026`,
# never read by season.py itself (which hardcodes nothing -- see that
# module's own docstring). A LIVE run against real Yahoo rosters ALWAYS
# gets these from src/cli.js's fresh settings fetch and passes them
# explicitly, which overrides this default outright; this constant is only
# ever reached when nobody supplied a real number to override it with.
_DEMO_PLAYOFF_START_WEEK = 16
_DEMO_END_WEEK = 17

# `tt trade --find`'s CLI-level speed/completeness defaults. `max_give`/
# `max_get` match `find_trades`' OWN kwarg defaults exactly (stated here
# too, not just inherited silently, so a reader of --help sees the real
# number); `screen_top` likewise matches its engine default. None of these
# override anything -- they exist purely so `tt.cli`'s own `--help` names a
# number instead of pointing a reader at trade.py. See `cmd_trade`'s
# docstring for why the screen stays ON by default (a person will wait for
# ~10s, not ~800s -- see that module's own docstring's "812 s" measurement
# for what disabling it (`--exhaustive`) can cost on a full-size league).
_TRADE_FIND_DEFAULT_MAX_GIVE = 2
_TRADE_FIND_DEFAULT_MAX_GET = 2
_TRADE_FIND_DEFAULT_SCREEN_TOP = 40


class CliError(Exception):
    """A user-facing input problem (missing file, bad flag, bad stdin) --
    distinct from an unexpected bug. `main` prints `str(this)` to stderr and
    exits 1; it never prints a traceback for one of these."""


# ---------------------------------------------------------------------------
# Shared loading helpers
# ---------------------------------------------------------------------------

def _load_league(path: Path) -> LeagueConfig:
    """Load league config from `path`, or fail with the exact remedy: run
    `tt league export`. Never invents a default league -- see the module
    this is called from (`analytics/data/league.json` is a real file this
    project's own `tt league export --out=...` writes; a missing one means
    the user hasn't run it yet, not that any fallback shape is safe to
    assume)."""
    if not path.exists():
        raise CliError(
            f"League config not found at {path}. Run: tt league export --out={path}"
        )
    try:
        return load_config(path)
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise CliError(f"League config at {path} is invalid: {e}") from e


def _load_history(data_dir: Path) -> pd.DataFrame:
    """Concatenate every `stats_player_week_*.parquet` in `data_dir`. Fails
    with a message naming the missing ingest step -- an empty/absent
    `data_dir` almost always means `tt sync`'s nflverse ingest has not
    been run, not that zero games have ever been played."""
    files = sorted(data_dir.glob("stats_player_week_*.parquet"))
    if not files:
        raise CliError(
            f"No nflverse parquet files found in {data_dir} "
            "(expected stats_player_week_<season>.parquet). "
            "Run the nflverse ingest to populate analytics/data/ first."
        )
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


_ADP_SEASON_RE = re.compile(r"ffc_adp_(\d+)\.json$")

# Where `tt sync` writes the live FFC feed (src/cache.js owns this path).
_DEFAULT_ADP_CACHE = Path.home() / ".tokentouchdowns" / "cache" / "ffc.json"

_ADP_COLUMNS = ("player_id", "adp", "stdev")


def _empty_adp() -> pd.DataFrame:
    return pd.DataFrame(columns=list(_ADP_COLUMNS))


def _parse_adp_payload(payload: dict) -> pd.DataFrame:
    # Season files (built for the backtest) key their rows "players"; the live
    # `tt sync` cache keys them "records". Same rows, different wrapper.
    players = payload.get("players") or payload.get("records") or []
    if not players:
        return _empty_adp()
    df = pd.DataFrame(players).rename(columns={"playerId": "player_id"})
    for column in _ADP_COLUMNS:
        if column not in df.columns:
            df[column] = pd.NA
    return df[list(_ADP_COLUMNS)]


def _load_adp(
    data_dir: Path,
    adp_path: str | None,
    live_cache: Path | None = None,
) -> tuple[pd.DataFrame, str | None]:
    """The ADP crosswalk to attach (`player_id`/`adp`/`stdev`), and a label
    for where it came from (for the output footer). `board`/`pick`/`mock`
    all still work with NO adp file at all -- `attach_adp` (reused from
    `tt.studies.draft_board`, not re-derived) simply leaves `adp`/`stdev`
    as NaN for every player, and `survival.add_survival`'s own documented
    convention ("no adp data at all: certainly available forever") takes
    it from there. This is the honest degrade the task brief asks for:
    never invent an ADP number for a league/season with no real feed
    cached yet.

    Resolution order, and the order matters more than it looks:

      1. an explicit `--adp` path
      2. THE LIVE CACHE written by `tt sync` (default
         `~/.tokentouchdowns/cache/ffc.json`) -- this season's real market
      3. the highest-numbered `ffc_adp_<season>.json` in `data_dir`, LABELLED
         STALE, because those files exist for the BACKTEST

    Step 2 was missing, and the omission was expensive. `analytics/data/`
    holds `ffc_adp_2023/2024/2025.json`, created to grade historical drafts.
    They look exactly like a current-season file, sort highest, and were
    silently preferred -- so the live board ranked against LAST SEASON'S
    market while looking entirely plausible. Measured against the real files:
    median ADP error 19.6 picks, 89 of 131 shared players off by more than a
    full round (Alvin Kamara 39 vs 158, Cam Skattebo 131 vs 40), and 102
    players in the actual 2026 market -- this year's rookies among them --
    absent from the board altogether.

    Falling back is still legitimate when no live cache exists yet, but it is
    no longer silent: the returned label names the season and says it is
    stale, so every surface that shows provenance can warn.
    """
    if adp_path:
        p = Path(adp_path)
        if not p.exists():
            raise CliError(f"ADP file not found: {p}")
        return _parse_adp_payload(json.loads(p.read_text())), str(p)

    cache = Path(live_cache) if live_cache is not None else _DEFAULT_ADP_CACHE
    if cache.exists():
        try:
            raw = json.loads(cache.read_text())
        except (json.JSONDecodeError, OSError):
            raw = None
        if raw is not None:
            # `tt sync` wraps the payload as {fetchedAt, data}; accept both.
            payload = raw.get("data", raw) if isinstance(raw, dict) else {}
            frame = _parse_adp_payload(payload)
            if not frame.empty:
                meta = payload.get("meta") or {}
                drafts = meta.get("totalDrafts") or meta.get("total_drafts")
                teams = meta.get("teams")
                label = "live ffc cache"
                if drafts:
                    label += f" ({drafts} drafts"
                    label += f", {teams}-team)" if teams else ")"
                return frame, label

    candidates = sorted(data_dir.glob("ffc_adp_*.json"))
    if not candidates:
        return _empty_adp(), None

    def _season(path: Path) -> int:
        m = _ADP_SEASON_RE.search(path.name)
        return int(m.group(1)) if m else -1

    best = max(candidates, key=_season)
    season = _season(best)
    # Named STALE on purpose. This file exists to grade a past draft; using it
    # to rank a live one is a fallback, and every surface that prints
    # provenance should be able to say so.
    label = f"STALE historical ADP -- {best.name} ({season} season), no live cache found"
    return _parse_adp_payload(json.loads(best.read_text())), label


def _train_seasons(history: pd.DataFrame, season: int) -> tuple[int, ...]:
    present = sorted({int(s) for s in history["season"].unique()})
    train = tuple(s for s in present if s < season)
    if not train:
        raise CliError(
            f"No seasons strictly before {season} in the loaded history "
            f"(loaded seasons: {present}) -- cannot project {season} without "
            "lookahead. Pass --season explicitly, or check --data-dir."
        )
    return train


def _default_season(history: pd.DataFrame) -> int:
    """One past the most recent season in the loaded history -- "next
    season to be played," with no dependence on the wall clock, so this
    module's behaviour for a given `--data-dir` is reproducible regardless
    of what day it is actually run."""
    return int(history["season"].max()) + 1


def _build_board(
    history: pd.DataFrame, config: LeagueConfig, teams: int, season: int,
    ffc: pd.DataFrame, n: int, seed: int | None,
) -> pd.DataFrame:
    """`project_players` (trained on every season strictly before `season`)
    + `attach_adp` (reused, tested) + `add_vor` for `teams`. This is this
    module's own small composition of EXISTING pieces -- not a new
    pipeline -- kept here (rather than reusing
    `tt.studies.draft_board.build_projection_board`) only so `--mc-n`/
    `--seed` can reach `project_players` directly; that backtest-oriented
    function has no way to pass either through.
    """
    projections = project_players(
        history, config, seasons=_train_seasons(history, season), n=n, seed=seed,
    )
    board = attach_adp(projections, ffc)
    return add_vor(board, config, teams=teams)


def _records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> JSON-safe list of dicts. Goes through `DataFrame.to_json`
    (not a manual `.to_dict("records")` + `json.dumps`) specifically because
    a manual round-trip leaves `NaN` as a literal Python float `nan`, which
    `json.dumps` serialises as the bare token `NaN` -- invalid JSON that
    silently parses in JS via a non-standard extension but breaks any
    strict parser. `to_json` converts NaN/NaT to `null` and numpy scalar
    types to native ones correctly."""
    if df.empty:
        return []
    return json.loads(df.to_json(orient="records"))


def _roster_df(roster: list[dict], proj_by_id: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Join a Node-supplied roster (already crosswalk-resolved to nflverse
    `player_id`, or `None` where Node could not resolve one) to this
    season's per-game projected points.

    THE PER-GAME SIMPLIFICATION. `project_players` returns a SEASON total
    (`proj_points`, `sd`); `lineup`/`playoff` need one week's worth. Neither
    is a function this engine has (the "existing modules to call" list this
    command was built against has no weekly/opponent-adjusted projector),
    so the per-game estimate used here is `proj_points / proj_games`
    (an average week) and, for variance, `sd / sqrt(proj_games)` -- valid
    under the same "independent identically-distributed weeks" assumption
    `playoff.py`'s own docstring already states plainly for its Normal-sum
    model. This is a real, named simplification (no bye-week/opponent/injury
    adjustment), not a hidden one.

    Returns `(roster_df, unprojected_names)` -- the second list is every
    roster entry with NO usable projection, whether because `player_id` was
    never resolved (`None`) or because the resolved id simply isn't in this
    engine's projectable universe (QB/RB/WR/TE only -- a K, a DEF, or a
    rookie with no history). Both cases are named, never silently dropped:
    they still appear as a row in `roster_df` with `proj_points = None`, so
    `lineup.optimal_lineup`'s own NaN-points handling benches them (or marks
    their slot empty) rather than crashing or vanishing them from the
    response.
    """
    rows: list[dict] = []
    unprojected: list[str] = []
    for p in roster:
        pid = p.get("player_id")
        row = {"player_id": pid, "name": p.get("name"), "position": p.get("position")}
        proj = proj_by_id.loc[pid] if pid is not None and pid in proj_by_id.index else None
        if proj is not None:
            games = float(proj["proj_games"])
            if games > 0:
                row["proj_points"] = float(proj["proj_points"]) / games
                row["sd"] = float(proj["sd"]) / (games ** 0.5)
            else:
                proj = None
        if proj is None:
            row["proj_points"] = None
            row["sd"] = None
            unprojected.append(p.get("name") or pid or "(unknown player)")
        rows.append(row)
    return pd.DataFrame(rows), unprojected


def _mock_draft_rosters(
    history: pd.DataFrame, config: LeagueConfig, ffc: pd.DataFrame,
    season: int, teams: int, rounds: int, mc_n: int, seed: int | None,
) -> dict[str, list[dict]]:
    """One simulated snake draft (`mock.simulate_draft`) against this
    league's own projected player pool, returned as `{"Mock Team N": [...]}`
    -- the exact DEMONSTRATION rosters `cmd_season`'s `--mock-draft` branch
    builds, factored out here so `cmd_trade`'s own `--mock-draft` support
    builds the SAME rosters the SAME way rather than a second copy of the
    same few lines.

    DETERMINISM IS LOAD-BEARING, not incidental. `cmd_trade`'s
    `--rosters-only` two-call flow (see that function's docstring) depends
    on calling this function TWICE with the same `seed` and getting back
    IDENTICAL rosters both times -- exactly the same guarantee `draft_seed`
    already gave `cmd_season` (defaults to 2026 when `seed` is `None`, so
    a run with no explicit `--seed` is still reproducible, not merely "the
    same within one process").
    """
    board = _build_board(history, config, teams, season, ffc, n=mc_n, seed=seed)
    draft_seed = seed if seed is not None else 2026
    picks = simulate_draft(
        board, teams=teams, rounds=rounds, my_slot=0,
        strategy=strategy_vor_survival(config, rounds=rounds),
        seed=draft_seed, return_all=True,
    )
    return {
        f"Mock Team {int(slot) + 1}": group.to_dict("records")
        for slot, group in picks.groupby("slot")
    }


def _season_roster_df(roster: list[dict], weekly_ids: set) -> tuple[pd.DataFrame, list[str]]:
    """Build the bare `(player_id, name, position)` frame `season.
    simulate_season` joins against a league-wide `weekly.project_week`
    board -- unlike `_roster_df` above, this does NOT pre-compute
    `proj_points`/`sd` itself (season.py's own `_joined_roster` does that
    join, choosing the marginal/conditional view in exactly one place; see
    that module's docstring). Same never-drop discipline as `_roster_df`:
    a roster entry Node could not resolve (`player_id: None`) or one this
    engine has no weekly projection for (not in `weekly_ids` -- a K/DEF, or
    simply absent from history) still gets a row, and is named in the
    returned `unprojected` list rather than silently vanishing from the
    simulated roster.
    """
    rows: list[dict] = []
    unprojected: list[str] = []
    for p in roster:
        pid = p.get("player_id")
        rows.append({"player_id": pid, "name": p.get("name"), "position": p.get("position")})
        if pid is None or pid not in weekly_ids:
            unprojected.append(p.get("name") or pid or "(unknown player)")
    return pd.DataFrame(rows, columns=["player_id", "name", "position"]), unprojected


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_board(args: argparse.Namespace, stdin_payload: dict) -> dict:
    config = _load_league(Path(args.config))
    data_dir = Path(args.data_dir)
    history = _load_history(data_dir)
    teams = args.teams or config.num_teams
    season = args.season or _default_season(history)
    ffc, adp_source = _load_adp(data_dir, args.adp, getattr(args, 'adp_cache', None))

    board = _build_board(history, config, teams, season, ffc, n=args.mc_n, seed=args.seed)

    slot = args.slot
    pick = next_pick = None
    if slot is not None:
        if not (1 <= slot <= teams):
            raise CliError(f"--slot must be between 1 and {teams} (got {slot})")
        slot0 = slot - 1
        pick = _pick_number(teams, 0, slot0)
        next_pick = _pick_number(teams, 1, slot0)
        board = add_survival(board, pick, next_pick, conditional=bool(args.conditional))

    board = board.sort_values("vor", ascending=False, na_position="last", kind="mergesort")
    count = args.count or DEFAULT_BOARD_COUNT
    return {
        "season": season,
        "teams": teams,
        "slot": slot,
        "pick": pick,
        "next_pick": next_pick,
        "adp_source": adp_source,
        "players": _records(board.head(count)),
    }


def cmd_pick(args: argparse.Namespace, stdin_payload: dict) -> dict:
    if args.slot is None:
        raise CliError("--slot is required (which draft slot are you picking from)")
    config = _load_league(Path(args.config))
    data_dir = Path(args.data_dir)
    history = _load_history(data_dir)
    teams = args.teams or config.num_teams
    if not (1 <= args.slot <= teams):
        raise CliError(f"--slot must be between 1 and {teams} (got {args.slot})")
    season = args.season or _default_season(history)
    ffc, adp_source = _load_adp(data_dir, args.adp)

    board = _build_board(history, config, teams, season, ffc, n=args.mc_n, seed=args.seed)

    slot0 = args.slot - 1
    round_index = args.round - 1
    pick = _pick_number(teams, round_index, slot0)
    next_pick = _pick_number(teams, round_index + 1, slot0)
    board = add_survival(board, pick, next_pick, conditional=bool(args.conditional))

    roster = stdin_payload.get("roster") or []
    rounds_remaining = max(args.rounds - args.round + 1, 0)
    ranked = recommend(
        board, pick, next_pick, roster, config, teams, rounds_remaining, n=args.n,
    )
    return {
        "season": season,
        "teams": teams,
        "slot": args.slot,
        "round": args.round,
        "pick": pick,
        "next_pick": next_pick,
        "adp_source": adp_source,
        "recommendations": _records(ranked),
    }


def _strategy_map(config: LeagueConfig, rounds: int, names: list[str]) -> dict[str, Strategy]:
    available: dict[str, Strategy] = {
        "adp": strategy_adp,
        "vor": strategy_vor,
        "vor_survival": strategy_vor_survival(config, rounds=rounds),
    }
    unknown = [n for n in names if n not in available]
    if unknown:
        raise CliError(
            f"Unknown --strategy value(s) {unknown}. Available: {list(available)}"
        )
    return {name: available[name] for name in names}


def cmd_mock(args: argparse.Namespace, stdin_payload: dict) -> dict:
    config = _load_league(Path(args.config))
    data_dir = Path(args.data_dir)
    history = _load_history(data_dir)
    teams = args.teams or config.num_teams
    season = args.season or _default_season(history)
    ffc, adp_source = _load_adp(data_dir, args.adp)

    rounds = args.rounds or DEFAULT_ROUNDS
    board = _build_board(history, config, teams, season, ffc, n=args.mc_n, seed=args.seed)

    names = [s.strip() for s in (args.strategy or ",".join(STRATEGY_NAMES)).split(",") if s.strip()]
    strategies = _strategy_map(config, rounds, names)

    slot = args.slot or (teams // 2)
    if not (0 <= slot < teams):
        raise CliError(f"--slot must be between 1 and {teams} (got {args.slot})")
    trials = args.trials or DEFAULT_TRIALS
    seed = args.seed if args.seed is not None else 2026

    comparison = compare_strategies(
        board, strategies, trials=trials, teams=teams, my_slot=slot, seed=seed,
        rounds=rounds, adp_noise=args.adp_noise, config=config,
    )
    return {
        "season": season,
        "teams": teams,
        "slot": slot + 1,
        "rounds": rounds,
        "trials": trials,
        "adp_source": adp_source,
        "note": (
            "Strategies are graded on this engine's own proj_points (the "
            "optimal starting lineup's projected total) -- there is no "
            "actual-outcome data for a season that hasn't happened yet. "
            "See docs/draft-engine-design.md's own circularity warning; "
            "this comparison shows relative behaviour, not a validated edge."
        ),
        "strategies": _records(comparison),
    }


def cmd_lineup(args: argparse.Namespace, stdin_payload: dict) -> dict:
    roster = stdin_payload.get("roster") or []
    if not roster:
        raise CliError("No roster provided (stdin JSON must include a non-empty 'roster' array)")
    config = _load_league(Path(args.config))
    data_dir = Path(args.data_dir)
    history = _load_history(data_dir)
    season = args.season or _default_season(history)
    projections = project_players(
        history, config, seasons=_train_seasons(history, season), n=args.mc_n, seed=args.seed,
    )
    proj_by_id = projections.set_index("player_id")

    roster_df, unprojected = _roster_df(roster, proj_by_id)
    lineup = optimal_lineup(roster_df, config, points_column="proj_points")
    return {
        "season": season,
        "week": args.week,
        "lineup": _records(lineup),
        "unprojected_players": unprojected,
    }


def cmd_playoff(args: argparse.Namespace, stdin_payload: dict) -> dict:
    roster = stdin_payload.get("roster") or []
    opponent_roster = stdin_payload.get("opponent_roster") or []
    if not roster:
        raise CliError("No roster provided (stdin JSON must include a non-empty 'roster' array)")
    if not opponent_roster:
        raise CliError(
            "No opponent roster provided (stdin JSON must include a non-empty "
            "'opponent_roster' array) -- playoff needs an opponent to simulate against"
        )
    config = _load_league(Path(args.config))
    data_dir = Path(args.data_dir)
    history = _load_history(data_dir)
    season = args.season or _default_season(history)
    projections = project_players(
        history, config, seasons=_train_seasons(history, season), n=args.mc_n, seed=args.seed,
    )
    proj_by_id = projections.set_index("player_id")

    roster_df, unprojected = _roster_df(roster, proj_by_id)
    opp_df, opp_unprojected = _roster_df(opponent_roster, proj_by_id)
    opp_lineup = optimal_lineup(opp_df, config, points_column="proj_points")

    result = playoff_lineup(
        roster_df, opp_lineup, config, n=args.sim_n, seed=args.seed,
        points_column="proj_points", sd_column="sd",
    )
    return {
        "season": season,
        "week": args.week,
        "lineup": _records(result),
        "win_probability": result.attrs["win_probability"],
        "expected_points_lineup_win_probability": result.attrs["expected_points_lineup_win_probability"],
        "expected_points_lineup_points": result.attrs["expected_points_lineup_points"],
        "playoff_lineup_points": result.attrs["playoff_lineup_points"],
        "unprojected_players": unprojected,
        "opponent_unprojected_players": opp_unprojected,
    }


def cmd_season(args: argparse.Namespace, stdin_payload: dict) -> dict:
    """Championship odds for the whole league via `season.simulate_season`.

    ROSTERS come from one of three mutually exclusive sources:
      - stdin `{"rosters": {team_key: [roster entries...]}}` -- the live
        path, one entry per Yahoo team, resolved to nflverse `player_id`
        by src/cli.js exactly as `lineup`/`playoff` already do.
      - `--mock-draft` -- a DEMONSTRATION: `mock.simulate_draft` runs one
        simulated snake draft against this league's own projected player
        pool (`_build_board`, reused from `cmd_board`) and its `--teams`
        (or the league's own `num_teams`) resulting rosters, labelled
        "Mock Team N", are simulated instead. Never confusable with a real
        answer -- the response's `"source"` is `"mock_draft"`, and
        src/cli.js prints an explicit banner.
      - stdin rosters supplied from a `--rosters=PATH` file (src/cli.js's
        job to read and forward as the same `{"rosters": ...}` shape) --
        this function does not know the difference between that and a
        live Node fetch; both arrive the same way on stdin.

    EVERY ROSTER EMPTY (the predraft case this league is actually in right
    now) is a `CliError`, never a table of zeros that could be mistaken for
    a real answer -- see the module docstring's general "never invent a
    default" discipline and the task brief's explicit predraft requirement.

    SCHEDULE: stdin `"schedule"` (Node's real `league/{key}/scoreboard`
    fetch, one entry per matchup) when given; `season.round_robin_schedule`
    over `playoff_start_week - 1` weeks otherwise -- the exact fallback
    `season.py`'s own module docstring names for when Yahoo has none.

    PLAYOFF SETTINGS (`--playoff-start-week`/`--end-week`/`--playoff-teams`/
    `--reseed`) are never assumed by `season.py` itself (see that module's
    docstring) and are forwarded here from whatever the caller passed --
    live Yahoo settings on a real run, or the `_DEMO_*` fallback documented
    above when nobody supplied real ones (only reachable via `--mock-draft`
    /`--rosters` in practice, since src/cli.js's live path always passes
    the real numbers explicitly).
    """
    if args.mock_draft and stdin_payload.get("rosters"):
        raise CliError(
            "--mock-draft and stdin rosters are mutually exclusive -- pass one or the other "
            "(a live/--rosters roster payload, or --mock-draft to simulate one)."
        )

    config = _load_league(Path(args.config))
    data_dir = Path(args.data_dir)
    history = _load_history(data_dir)
    season = args.season or _default_season(history)
    reseed = True if args.reseed is None else bool(args.reseed)

    adp_source = None
    if args.mock_draft:
        ffc, adp_source = _load_adp(data_dir, args.adp)
        teams = args.teams or config.num_teams
        rounds = args.rounds or DEFAULT_ROUNDS
        rosters_payload = _mock_draft_rosters(
            history, config, ffc, season, teams, rounds, args.mc_n, args.seed,
        )
        source = "mock_draft"
    else:
        rosters_payload = stdin_payload.get("rosters") or {}
        source = "live"

    if not rosters_payload or all(not roster for roster in rosters_payload.values()):
        raise CliError(
            "No rosters to simulate -- every roster is empty. This is expected before a draft "
            "has happened (a predraft league): there is nothing to simulate yet. Run `tt season` "
            "again once your real draft is complete, or exercise this command right now with "
            "--mock-draft (a simulated draft against this league's own player pool) or "
            "--rosters=PATH (your own hand-built hypothetical rosters)."
        )

    weekly_board = project_week(
        history, config, seasons=_train_seasons(history, season),
        n=args.mc_n, seed=args.seed,
        as_of_season=season if args.week is not None else None,
        as_of_week=args.week,
    )
    weekly_ids = set(weekly_board["player_id"])

    rosters: dict[str, pd.DataFrame] = {}
    unprojected_by_team: dict[str, list[str]] = {}
    for team, roster in rosters_payload.items():
        df, unprojected = _season_roster_df(roster, weekly_ids)
        rosters[team] = df
        unprojected_by_team[team] = unprojected

    schedule_payload = stdin_payload.get("schedule")
    if schedule_payload:
        schedule = pd.DataFrame(schedule_payload)
    else:
        schedule = round_robin_schedule(list(rosters), weeks=args.playoff_start_week - 1)

    result = simulate_season(
        rosters, schedule, config, weekly=weekly_board,
        playoff_start_week=args.playoff_start_week, end_week=args.end_week,
        playoff_teams=args.playoff_teams, reseed=reseed, n=args.n, seed=args.seed,
    )
    return {
        "season": season,
        "week": args.week,
        "source": source,
        "adp_source": adp_source,
        "n": result.attrs["n"],
        "monte_carlo_se": result.attrs["monte_carlo_se"],
        "playoff_start_week": result.attrs["playoff_start_week"],
        "end_week": result.attrs["end_week"],
        "playoff_teams": result.attrs["playoff_teams"],
        "reseed": result.attrs["reseed"],
        "regular_season_weeks": result.attrs["regular_season_weeks"],
        "teams": _records(result),
        "unprojected_players": unprojected_by_team,
    }


def _estimate_trade_candidates(
    rosters: Mapping[str, pd.DataFrame], my_team: str, opponents: list[str],
    max_give: int, max_get: int,
) -> int:
    """The EXACT number of candidate trades `find_trades` will enumerate
    against `opponents` before any screening -- `math.comb` over roster
    sizes, the same arithmetic `tt.trade`'s own module docstring uses to
    state "24843 candidate trades" for a 13-man/2-for-2/3-counterparty
    league. This is CLI-level bookkeeping, not a modelling decision (same
    category as `_pick_number` -- see the module docstring): the
    combinatorics are deterministic, so this can be computed cheaply BEFORE
    the expensive call and printed to stderr as an honest up-front
    estimate, since `find_trades` itself has no per-candidate progress
    callback to report real progress with (see `cmd_trade`'s docstring)."""
    my_n = len(rosters[my_team])
    total = 0
    for opponent in opponents:
        their_n = len(rosters[opponent])
        give_count = sum(math.comb(my_n, k) for k in range(1, max_give + 1) if k <= my_n)
        get_count = sum(math.comb(their_n, k) for k in range(1, max_get + 1) if k <= their_n)
        total += give_count * get_count
    return total


def cmd_trade(args: argparse.Namespace, stdin_payload: dict, stderr: IO[str]) -> dict:
    """Trade valuation via `trade.evaluate_trade` / `trade.find_trades`
    (docs/draft-engine-design.md's phase-3 trade section). Mirrors
    `cmd_season` in almost every particular -- the SAME roster/schedule
    input channels, the SAME predraft guard, the SAME `--mock-draft`/
    `--rosters=PATH` offline demonstration paths -- because a trade is
    valued against exactly the inputs a season is (see `tt.trade`'s own
    module docstring: "a trade is worth the change it makes to
    championship probability").

    TWO CALL SHAPES, selected by `--find`:
      `--find` -- `trade.find_trades`. Stdin needs only `rosters` (+
      optional `schedule`) and `my_team`; an optional `their_team` narrows
      the search to one counterparty (`--with=TEAM` on the Node side),
      exactly like `find_trades`' own `their_teams`. Returns one row per
      SIMULATED candidate, ranked.
      otherwise -- `trade.evaluate_trade`. Stdin additionally needs
      `their_team`, `i_give`, `i_get` -- player ids ALREADY resolved by
      Node (exactly as it resolves `lineup`/`playoff`/`season` rosters;
      see this module's own docstring and `src/identity.js`). Returns
      exactly 2 rows.

    `--rosters-only`: build and return `rosters` WITHOUT evaluating
    anything -- the one piece of bookkeeping this command needs beyond
    `cmd_season`'s. `--give`/`--get` are typed by the user as PLAYER NAMES
    on the command line, and Node resolves a name to a `player_id` by
    matching it against the actual roster -- but under `--mock-draft` that
    roster does not exist until THIS module builds it
    (`_mock_draft_rosters`), so Node cannot see it in advance the way it
    can a live or `--rosters=PATH` roster. The fix is a two-call, not a
    rewrite: Node calls this command once with `--rosters-only` to fetch
    the deterministic, `--seed`-pinned rosters, resolves names against
    them locally exactly as it would a live roster, then calls again
    (same seed) with the resolved `i_give`/`i_get` -- `_mock_draft_rosters`
    reproduces IDENTICAL rosters both times (see that function's own
    docstring; pinned here by
    `test_trade_rosters_only_is_deterministic_under_the_same_seed` and
    `test_trade_mock_draft_two_call_flow_evaluates_the_rosters_it_resolved`).
    Live and `--rosters=PATH` rosters never need this round trip: Node
    already holds them before it ever calls this module.

    PROGRESS. `find_trades` costs one season simulation per SURVIVING
    candidate (its own docstring: up to minutes for an exhaustive
    multi-player search) and exposes no incremental callback -- its
    signature is frozen, and re-implementing its inner loop here just to
    fake one would be a second model, exactly what `tt.trade` itself
    refuses to do for `simulate_season` (see its "WHAT IS REUSED" section).
    So: an honest UP-FRONT ESTIMATE to stderr before the one blocking call,
    not a fabricated progress bar -- `_estimate_trade_candidates` is exact
    (deterministic combinatorics), and actual cost is governed directly by
    the `--max-give`/`--max-get`/`--screen-top`/`--exhaustive`/`--n` flags
    this estimate names.

    UNCERTAINTY. Every row this returns (`evaluate_trade`'s `sides`,
    `find_trades`' `candidates`) already carries `delta_se`/
    `delta_ci_low`/`delta_ci_high`/`significant` from the engine untouched
    -- this module adds no rounding, no "significant"-only view, and drops
    nothing. Rendering the interval so a sub-noise delta cannot read as
    signal is `src/cli.js`'s job (see the task's own "OUTPUT REQUIREMENT
    THAT MATTERS MOST"), which needs the raw numbers, not a pre-formatted
    string.
    """
    if args.mock_draft and stdin_payload.get("rosters"):
        raise CliError(
            "--mock-draft and stdin rosters are mutually exclusive -- pass one or the other "
            "(a live/--rosters roster payload, or --mock-draft to simulate one)."
        )

    config = _load_league(Path(args.config))
    data_dir = Path(args.data_dir)
    history = _load_history(data_dir)
    season = args.season or _default_season(history)

    adp_source = None
    if args.mock_draft:
        ffc, adp_source = _load_adp(data_dir, args.adp)
        teams = args.teams or config.num_teams
        rounds = args.rounds or DEFAULT_ROUNDS
        rosters_payload = _mock_draft_rosters(
            history, config, ffc, season, teams, rounds, args.mc_n, args.seed,
        )
        source = "mock_draft"
    else:
        rosters_payload = stdin_payload.get("rosters") or {}
        source = "live"

    if not rosters_payload or all(not roster for roster in rosters_payload.values()):
        raise CliError(
            "No rosters to trade against -- every roster is empty. This is expected before a "
            "draft has happened (a predraft league): there is nothing to trade yet. Run `tt "
            "trade` again once your real draft is complete, or exercise this command right now "
            "with --mock-draft (a simulated draft against this league's own player pool) or "
            "--rosters=PATH (your own hand-built hypothetical rosters)."
        )

    if args.rosters_only:
        # SLIMMED to exactly the `(player_id, name, position)` shape a live
        # or `--rosters=PATH` roster already has -- `rosters_payload` from
        # `_mock_draft_rosters` carries every column the draft board/picks
        # DataFrame does (VOR, tier, survival stats, ADP...), and
        # `attach_adp` leaves `adp`/`stdev` (and anything survival derives
        # from them) as literal NaN for a player this league's ADP feed
        # doesn't cover (see `_load_adp`'s "never invent an ADP number").
        # Handed to `json.dumps` UNMODIFIED, a raw NaN serialises as the
        # bare, non-standard `NaN` token -- exactly the failure mode
        # `_records`'s own docstring documents for a manual (non-`to_json`)
        # dict round-trip, and one Node's strict `JSON.parse` rejects
        # outright (Python's own `json.loads` accepts it non-strictly,
        # which is why this has to be caught here, not downstream).
        slim = {
            team: [
                {"player_id": p.get("player_id"), "name": p.get("name"), "position": p.get("position")}
                for p in roster
            ]
            for team, roster in rosters_payload.items()
        }
        return {"season": season, "source": source, "adp_source": adp_source, "rosters": slim}

    weekly_board = project_week(
        history, config, seasons=_train_seasons(history, season),
        n=args.mc_n, seed=args.seed,
        as_of_season=season if args.week is not None else None,
        as_of_week=args.week,
    )
    weekly_ids = set(weekly_board["player_id"])

    rosters: dict[str, pd.DataFrame] = {}
    unprojected_by_team: dict[str, list[str]] = {}
    for team, roster in rosters_payload.items():
        df, unprojected = _season_roster_df(roster, weekly_ids)
        rosters[team] = df
        unprojected_by_team[team] = unprojected

    schedule_payload = stdin_payload.get("schedule")
    if schedule_payload:
        schedule = pd.DataFrame(schedule_payload)
    else:
        schedule = round_robin_schedule(list(rosters), weeks=args.playoff_start_week - 1)

    reseed = True if args.reseed is None else bool(args.reseed)
    playoff_teams_reported = args.playoff_teams if args.playoff_teams is not None else len(rosters)
    common = dict(
        weekly=weekly_board, playoff_start_week=args.playoff_start_week,
        end_week=args.end_week, playoff_teams=args.playoff_teams,
        reseed=reseed, n=args.n, seed=args.seed,
    )

    if args.find:
        my_team = stdin_payload.get("my_team")
        if not my_team:
            raise CliError(
                "stdin JSON must include 'my_team' (which team's championship odds to search for)"
            )
        if my_team not in rosters:
            raise CliError(f"my_team {my_team!r} has no roster in the payload")
        their_team = stdin_payload.get("their_team")
        if their_team is not None and their_team not in rosters:
            raise CliError(f"their_team {their_team!r} has no roster in the payload")
        their_teams = [their_team] if their_team else None

        screen_top = None if args.exhaustive else args.screen_top
        opponents = their_teams if their_teams is not None else [t for t in rosters if t != my_team]
        estimated = _estimate_trade_candidates(rosters, my_team, opponents, args.max_give, args.max_get)
        stderr.write(
            f"tt trade --find: up to {estimated} candidate trade(s) across {len(opponents)} "
            f"counterpart{'y' if len(opponents) == 1 else 'ies'} "
            f"(max_give={args.max_give}, max_get={args.max_get}) at n={args.n}. "
            + (
                f"Screened to the top {screen_top} per side per counterparty before simulating "
                "(fast, but can miss a mutually-beneficial multi-player package -- see tt.trade's "
                "own docstring; pass --exhaustive to disable the screen and simulate every "
                "candidate instead). "
                if screen_top is not None else
                "SCREEN DISABLED (--exhaustive): every candidate above will be simulated exactly "
                "-- this can take minutes on a full-size roster (tt.trade's own docstring measured "
                "812s for an exhaustive 2-for-2). "
            )
            + "This is a single blocking call with no incremental progress to report.\n"
        )
        try:
            result = find_trades(
                rosters, schedule, config, my_team=my_team, max_give=args.max_give,
                max_get=args.max_get, their_teams=their_teams, screen_top=screen_top, **common,
            )
        except KeyError as e:
            raise CliError(f"Invalid trade search: {e.args[0] if e.args else e}") from e
        except ValueError as e:
            raise CliError(f"Invalid trade search: {e}") from e
        return {
            "mode": "find", "season": season, "source": source, "adp_source": adp_source,
            "my_team": my_team,
            "n": result.attrs["n"], "monte_carlo_se": result.attrs["monte_carlo_se"],
            "playoff_start_week": args.playoff_start_week, "end_week": args.end_week,
            "playoff_teams": playoff_teams_reported, "reseed": reseed,
            "max_give": result.attrs["max_give"], "max_get": result.attrs["max_get"],
            "screen_top": result.attrs["screen_top"],
            "candidates_enumerated": result.attrs["candidates_enumerated"],
            "candidates_simulated": result.attrs["candidates_simulated"],
            "candidates": _records(result),
            "unprojected_players": unprojected_by_team,
        }

    my_team = stdin_payload.get("my_team")
    their_team = stdin_payload.get("their_team")
    i_give = stdin_payload.get("i_give") or []
    i_get = stdin_payload.get("i_get") or []
    if not my_team or not their_team:
        raise CliError("stdin JSON must include 'my_team' and 'their_team'")
    if my_team not in rosters:
        raise CliError(f"my_team {my_team!r} has no roster in the payload")
    if their_team not in rosters:
        raise CliError(f"their_team {their_team!r} has no roster in the payload")
    if not i_give and not i_get:
        raise CliError(
            "stdin JSON must include a non-empty 'i_give' and/or 'i_get' array "
            "(what you are trading) -- or pass --find to search for trades instead"
        )

    try:
        result = evaluate_trade(
            rosters, schedule, config, my_team=my_team, their_team=their_team,
            i_give=i_give, i_get=i_get, **common,
        )
    except KeyError as e:
        raise CliError(f"Invalid trade: {e.args[0] if e.args else e}") from e
    except ValueError as e:
        raise CliError(f"Invalid trade: {e}") from e

    return {
        "mode": "evaluate", "season": season, "source": source, "adp_source": adp_source,
        "my_team": my_team, "their_team": their_team,
        "n": result.attrs["n"], "monte_carlo_se": result.attrs["monte_carlo_se"],
        "playoff_start_week": result.attrs["playoff_start_week"],
        "end_week": result.attrs["end_week"],
        "playoff_teams": playoff_teams_reported, "reseed": result.attrs["reseed"],
        "sides": _records(result),
        "unprojected_players": unprojected_by_team,
    }


COMMANDS = {
    "board": cmd_board,
    "pick": cmd_pick,
    "mock": cmd_mock,
    "lineup": cmd_lineup,
    "playoff": cmd_playoff,
    "season": cmd_season,
    "trade": cmd_trade,
}


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------

def _add_data_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", default="data/league.json")
    p.add_argument("--data-dir", default="data")
    p.add_argument("--season", type=int, default=None)
    p.add_argument("--adp", default=None, help="Path to an ffc_adp_<season>.json crosswalk file")
    p.add_argument(
        "--adp-cache",
        help="Path to the live `tt sync` FFC cache. Defaults to ~/.tokentouchdowns/cache/ffc.json; point it at a missing file to force the historical fallback.",
    )
    p.add_argument("--mc-n", type=int, default=DEFAULT_MC_N, dest="mc_n",
                    help="Monte Carlo draws per player stream (project_players' own n)")
    p.add_argument("--seed", type=int, default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tt.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    board = sub.add_parser("board", help="Ranked draft board")
    _add_data_flags(board)
    board.add_argument("--teams", type=int, default=None)
    board.add_argument("--slot", type=int, default=None)
    board.add_argument("--count", type=int, default=None)
    board.add_argument("--conditional", action="store_true")

    pick = sub.add_parser("pick", help="What to take right now")
    _add_data_flags(pick)
    pick.add_argument("--teams", type=int, default=None)
    pick.add_argument("--slot", type=int, default=None)
    pick.add_argument("--round", type=int, default=1)
    pick.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    pick.add_argument("--conditional", action="store_true")
    pick.add_argument("--n", type=int, default=DEFAULT_PICK_N)

    mock = sub.add_parser("mock", help="Simulate and compare draft strategies")
    _add_data_flags(mock)
    mock.add_argument("--teams", type=int, default=None)
    mock.add_argument("--slot", type=int, default=None)
    mock.add_argument("--trials", type=int, default=None)
    mock.add_argument("--rounds", type=int, default=None)
    mock.add_argument("--strategy", default=None,
                       help=f"Comma-separated subset of {list(STRATEGY_NAMES)}")
    mock.add_argument("--adp-noise", type=float, default=ADP_NOISE_DEFAULT, dest="adp_noise")

    lineup = sub.add_parser("lineup", help="Optimal lineup for this week")
    _add_data_flags(lineup)
    lineup.add_argument("--week", type=int, default=None)

    playoff = sub.add_parser("playoff", help="Variance-aware lineup vs. an opponent")
    _add_data_flags(playoff)
    playoff.add_argument("--week", type=int, default=None)
    playoff.add_argument("--sim-n", type=int, default=PLAYOFF_DEFAULT_N, dest="sim_n")

    season = sub.add_parser("season", help="Championship odds via season simulation")
    _add_data_flags(season)
    season.add_argument("--n", type=int, default=SEASON_DEFAULT_N,
                         help="Season simulation count (season.simulate_season's own n)")
    season.add_argument("--week", type=int, default=None,
                         help="As-of week for a leakage-safe in-season projection")
    season.add_argument("--playoff-start-week", type=int, default=_DEMO_PLAYOFF_START_WEEK,
                         dest="playoff_start_week")
    season.add_argument("--end-week", type=int, default=_DEMO_END_WEEK, dest="end_week")
    season.add_argument("--playoff-teams", type=int, default=None, dest="playoff_teams")
    season.add_argument("--reseed", type=int, choices=[0, 1], default=None)
    season.add_argument("--teams", type=int, default=None, help="team count for --mock-draft")
    season.add_argument("--rounds", type=int, default=None, help="draft rounds for --mock-draft")
    season.add_argument("--mock-draft", action="store_true", dest="mock_draft",
                         help="DEMONSTRATION ONLY: simulate a draft instead of using live rosters")

    trade = sub.add_parser(
        "trade", help="Evaluate a trade, or search for one, by championship-probability delta")
    _add_data_flags(trade)
    trade.add_argument("--n", type=int, default=TRADE_DEFAULT_N,
                        help="Simulation count (trade.evaluate_trade/find_trades' own n)")
    trade.add_argument("--week", type=int, default=None,
                        help="As-of week for a leakage-safe in-season projection")
    trade.add_argument("--playoff-start-week", type=int, default=_DEMO_PLAYOFF_START_WEEK,
                        dest="playoff_start_week")
    trade.add_argument("--end-week", type=int, default=_DEMO_END_WEEK, dest="end_week")
    trade.add_argument("--playoff-teams", type=int, default=None, dest="playoff_teams")
    trade.add_argument("--reseed", type=int, choices=[0, 1], default=None)
    trade.add_argument("--teams", type=int, default=None, help="team count for --mock-draft")
    trade.add_argument("--rounds", type=int, default=None, help="draft rounds for --mock-draft")
    trade.add_argument("--mock-draft", action="store_true", dest="mock_draft",
                        help="DEMONSTRATION ONLY: simulate a draft instead of using live rosters")
    trade.add_argument("--rosters-only", action="store_true", dest="rosters_only",
                        help="Build and return rosters without evaluating a trade "
                             "(Node's --mock-draft player-name-resolution prefetch)")
    trade.add_argument("--find", action="store_true",
                        help="Search for trades that help my_team, instead of evaluating one")
    trade.add_argument("--max-give", type=int, default=_TRADE_FIND_DEFAULT_MAX_GIVE, dest="max_give")
    trade.add_argument("--max-get", type=int, default=_TRADE_FIND_DEFAULT_MAX_GET, dest="max_get")
    trade.add_argument("--screen-top", type=int, default=_TRADE_FIND_DEFAULT_SCREEN_TOP,
                        dest="screen_top")
    trade.add_argument("--exhaustive", action="store_true",
                        help="Disable find_trades' screen: simulate every enumerated candidate "
                             "exactly (can take minutes on a full-size roster; see tt.trade's "
                             "own docstring)")

    return parser


def _read_stdin_json(stdin: IO[str]) -> dict:
    raw = stdin.read()
    if not raw or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise CliError(f"stdin is not valid JSON: {e}") from e
    if not isinstance(payload, dict):
        raise CliError("stdin JSON must be an object (e.g. {\"roster\": [...]})")
    return payload


def main(
    argv: list[str] | None = None,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Entry point for `python -m tt.cli`. `stdin`/`stdout`/`stderr` are
    injectable (default to the real streams) so tests never touch the real
    process streams -- mirrors `src/client.js`'s injectable `fetch` on the
    Node side of this same bridge."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse itself already wrote usage/error text to stderr; just
        # translate its SystemExit into this function's own int-return
        # contract so callers never need to catch SystemExit.
        return int(e.code) if e.code is not None else 2

    try:
        payload = _read_stdin_json(stdin)
        handler = COMMANDS.get(args.command)
        if handler is None:
            raise CliError(f"Unknown command: {args.command}")
        # `cmd_trade` alone needs `stderr` (its `--find` up-front candidate
        # estimate -- see that function's own "PROGRESS" docstring section);
        # every other handler keeps the original 2-argument contract.
        result: dict[str, Any] = (
            handler(args, payload, stderr) if args.command == "trade" else handler(args, payload)
        )
    except CliError as e:
        stderr.write(f"{e}\n")
        return 1
    except Exception:
        # An unexpected bug, not a user input problem -- still never let it
        # reach stdout (see module docstring, "STDOUT DISCIPLINE"). The
        # traceback goes to stderr so it's visible for debugging, and
        # src/analytics.js reports "non-JSON/failed Python" rather than
        # trying to parse a traceback as JSON.
        import traceback
        traceback.print_exc(file=stderr)
        return 1

    stdout.write(json.dumps(result) + "\n")
    stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
