"""One-shot recompute for the live draft room: survival + recommendations
against an ALREADY-BUILT board (docs/draft-room-design.md section 3).

Deliberately NOT under analytics/ (branch instructions for this feature
treat analytics/ as off limits) and deliberately NOT a second
implementation of any fantasy logic -- every number produced here comes
from tt.survival.add_survival and tt.draft.recommend, imported unchanged
from the installed analytics venv (analytics/.venv, editable-installed, so
`import tt` resolves regardless of this script's own location or cwd).
This script exists ONLY to avoid re-running tt.projections.project_players'
~4-5s Monte Carlo -- which analytics/src/tt/cli.py's own `board`/`pick`
subcommands always do, since neither was built to accept a pre-built board
-- on every few-second poll during a live two-hour draft. See
src/draft-room.js's own module docstring for the Node side of this split:
`board` is called through src/analytics.js exactly ONCE, at startup; this
script is called, cheaply, on every poll and every manual mark/undo after
that.

Measured cost (analytics/data's real league, 1,849-player board): ~0.01s
for add_survival + recommend themselves, ~0.5-0.7s total including Python
/ pandas process startup -- dominated by interpreter startup, not
computation, and nowhere near the ~5s a full re-projection would cost.

PROTOCOL. Mirrors analytics/src/tt/cli.py's own "stdout discipline" (see
that module's docstring): read ONE JSON document from stdin, write ONE
JSON document to stdout, nothing else ever touches stdout.

stdin:
{
  "records": [...]        every row `tt.cli board`'s "players" array
                           returned at startup -- ALL players, drafted or
                           not, unchanged since startup (proj/vor/tier/adp
                           do not change during a draft; see design doc
                           section 3)
  "pick": <int>            the overall pick number happening right now
  "nextPick": <int>        the caller's own next pick (must be > pick)
  "availableIds": [...]    player_id values still undrafted -- recommend()
                            must only ever rank players actually on the
                            board (see tt.draft's own docstring)
  "roster": [...]          the caller's own drafted players so far, each
                            {"player_id", "position"}
  "config": {...}          the same shape analytics/data/league.json is
                            (leagueKey/numTeams/rosterSlots/scoring/...)
  "teams": <int>
  "roundsRemaining": <int> rounds left INCLUDING the one about to be
                            picked, for tt.draft's need-urgency signal
  "n": <int>                how many recommendations to return
  "conditional": <bool>     optional, default false -- forwarded to
                            add_survival unchanged
}

stdout:
{
  "board": [...every record in `records`, decorated with p_gone_by_next],
  "recommendations": [...top `n` rows from tt.draft.recommend]
}
"""
from __future__ import annotations

import json
import sys

import pandas as pd

from tt.draft import recommend
from tt.league import load_config_from_dict
from tt.survival import add_survival


def _records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> JSON-safe list of dicts, via `to_json` rather than a
    manual `.to_dict("records")` + `json.dumps` -- identical reasoning to
    (and deliberately mirroring) `tt.cli`'s own `_records` helper: a manual
    round-trip leaves NaN as a literal Python float, which `json.dumps`
    renders as the bare (invalid-JSON) token `NaN`."""
    if df.empty:
        return []
    return json.loads(df.to_json(orient="records"))


def main() -> int:
    payload = json.load(sys.stdin)
    records = payload.get("records") or []
    pick = payload["pick"]
    next_pick = payload["nextPick"]
    available_ids = set(payload.get("availableIds") or [])
    roster = payload.get("roster") or []
    config = load_config_from_dict(payload["config"])
    teams = payload["teams"]
    rounds_remaining = payload["roundsRemaining"]
    n = payload.get("n", 10)
    conditional = bool(payload.get("conditional", False))

    board = pd.DataFrame.from_records(records)
    board = add_survival(board, pick, next_pick, conditional=conditional)

    available = board[board["player_id"].isin(available_ids)]
    ranked = recommend(
        available, pick, next_pick, roster, config, teams, rounds_remaining, n=n,
    )

    print(json.dumps({
        "board": _records(board),
        "recommendations": _records(ranked),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
