"""Phase 2 CLI adapter (docs/draft-engine-design.md section 4).

`tt draft board`/`pick`/`mock`/`lineup`/`playoff` live on ONE Node CLI
surface (`bin/tt.js` -> `src/cli.js`), but the analytics engine is Python.
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
import re
import sys
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
    strategy_adp,
    strategy_vor,
    strategy_vor_survival,
)
from .playoff import DEFAULT_N as PLAYOFF_DEFAULT_N, playoff_lineup
from .projections import project_players
from .studies.draft_board import attach_adp
from .survival import add_survival
from .vor import add_vor

DEFAULT_MC_N = 5000
DEFAULT_TRIALS = 50
DEFAULT_BOARD_COUNT = 50
DEFAULT_PICK_N = 5
STRATEGY_NAMES = ("adp", "vor", "vor_survival")


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

_ADP_COLUMNS = ("player_id", "adp", "stdev")


def _empty_adp() -> pd.DataFrame:
    return pd.DataFrame(columns=list(_ADP_COLUMNS))


def _parse_adp_payload(payload: dict) -> pd.DataFrame:
    players = payload.get("players") or []
    if not players:
        return _empty_adp()
    df = pd.DataFrame(players).rename(columns={"playerId": "player_id"})
    for column in _ADP_COLUMNS:
        if column not in df.columns:
            df[column] = pd.NA
    return df[list(_ADP_COLUMNS)]


def _load_adp(data_dir: Path, adp_path: str | None) -> tuple[pd.DataFrame, str | None]:
    """The ADP crosswalk to attach (`player_id`/`adp`/`stdev`), and a label
    for where it came from (for the output footer). `board`/`pick`/`mock`
    all still work with NO adp file at all -- `attach_adp` (reused from
    `tt.studies.draft_board`, not re-derived) simply leaves `adp`/`stdev`
    as NaN for every player, and `survival.add_survival`'s own documented
    convention ("no adp data at all: certainly available forever") takes
    it from there. This is the honest degrade the task brief asks for:
    never invent an ADP number for a league/season with no real feed
    cached yet.

    Auto-detects the highest-season `ffc_adp_<season>.json` present in
    `data_dir` when `adp_path` isn't given explicitly -- `analytics/data/`
    is gitignored and never guaranteed to hold a file for the CURRENT
    season, so "most recent available" is a reasonable, clearly-labelled
    stand-in rather than a silent requirement this command would otherwise
    have no way to satisfy.
    """
    if adp_path:
        p = Path(adp_path)
        if not p.exists():
            raise CliError(f"ADP file not found: {p}")
        return _parse_adp_payload(json.loads(p.read_text())), str(p)

    candidates = sorted(data_dir.glob("ffc_adp_*.json"))
    if not candidates:
        return _empty_adp(), None

    def _season(path: Path) -> int:
        m = _ADP_SEASON_RE.search(path.name)
        return int(m.group(1)) if m else -1

    best = max(candidates, key=_season)
    return _parse_adp_payload(json.loads(best.read_text())), best.name


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


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_board(args: argparse.Namespace, stdin_payload: dict) -> dict:
    config = _load_league(Path(args.config))
    data_dir = Path(args.data_dir)
    history = _load_history(data_dir)
    teams = args.teams or config.num_teams
    season = args.season or _default_season(history)
    ffc, adp_source = _load_adp(data_dir, args.adp)

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


COMMANDS = {
    "board": cmd_board,
    "pick": cmd_pick,
    "mock": cmd_mock,
    "lineup": cmd_lineup,
    "playoff": cmd_playoff,
}


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------

def _add_data_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", default="data/league.json")
    p.add_argument("--data-dir", default="data")
    p.add_argument("--season", type=int, default=None)
    p.add_argument("--adp", default=None, help="Path to an ffc_adp_<season>.json crosswalk file")
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
        result: dict[str, Any] = handler(args, payload)
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
