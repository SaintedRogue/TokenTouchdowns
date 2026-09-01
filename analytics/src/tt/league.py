"""League configuration.

Scoring and roster slots are read from the league itself rather than hardcoded,
because replacement level -- and therefore every VOR number -- is a direct
function of starting slots and team count. A constant would silently produce a
wrong draft board in any other league.

Scoring is keyed by Yahoo stat id, not display name. Yahoo reuses display
names across stat groups -- most notably "Int", which is stat_id 6 (passing
interceptions, a QB penalty) AND stat_id 33 (defensive interceptions, a DEF
bonus). An earlier version of the export keyed scoring by name and let +2
silently clobber -1, which would have scored every thrown interception as a
QB point gain. Stat ids are stable identifiers in Yahoo's schema, so they are
the only safe join key.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# Yahoo stat id -> nflverse column name, for offensive skill players only.
# Defensive stats, kicking and return TDs have no corresponding column here;
# they are simply omitted -- this loader scores offense from nflverse data,
# and filtering positions out of that is the consumer's job, not this one's.
STAT_COLUMNS: dict[int, str] = {
    4: "passing_yards",
    5: "passing_tds",
    6: "passing_interceptions",
    9: "rushing_yards",
    10: "rushing_tds",
    11: "receptions",
    12: "receiving_yards",
    13: "receiving_tds",
}

# Yahoo has a single combined "fumbles lost" stat (id 18); nflverse instead
# splits fumbles lost by how the ball was lost: on a run, on a reception, or
# on a sack. Those three are mutually exclusive per play -- a given fumble can
# only be one of them -- so applying the same weight to all three is
# arithmetically equivalent to applying it once per fumble lost. It is NOT a
# triple penalty, even though it looks like one mapping -> many columns.
FUMBLES_LOST_STAT_ID = 18
FUMBLES_LOST_COLUMNS: tuple[str, ...] = (
    "rushing_fumbles_lost",
    "receiving_fumbles_lost",
    "sack_fumbles_lost",
)

# Which real positions a flex slot can be filled by.
FLEX_ELIGIBLE: dict[str, tuple[str, ...]] = {
    "W/R/T": ("RB", "WR", "TE"),
    "W/R": ("RB", "WR"),
    "Q/W/R/T": ("QB", "RB", "WR", "TE"),
}


@dataclass(frozen=True)
class LeagueConfig:
    league_key: str
    name: str
    num_teams: int
    max_teams: int
    draft_status: str
    roster_slots: dict[str, int]
    scoring: list[dict]


def load_config_from_dict(raw: dict) -> LeagueConfig:
    return LeagueConfig(
        league_key=raw["leagueKey"], name=raw["name"],
        num_teams=int(raw["numTeams"]), max_teams=int(raw["maxTeams"]),
        draft_status=raw.get("draftStatus", ""),
        roster_slots=dict(raw["rosterSlots"]), scoring=list(raw["scoring"]),
    )


def load_config(path: str | Path) -> LeagueConfig:
    return load_config_from_dict(json.loads(Path(path).read_text()))


def scoring_weights(config: LeagueConfig) -> dict[str, float]:
    """League scoring keyed by nflverse column name.

    Keyed by stat id (see module docstring), not display name -- Yahoo's own
    display names collide across stat groups.
    """
    out: dict[str, float] = {}
    for stat in config.scoring:
        stat_id = stat["statId"]
        value = float(stat["value"])
        if stat_id == FUMBLES_LOST_STAT_ID:
            for column in FUMBLES_LOST_COLUMNS:
                out[column] = value
        elif stat_id in STAT_COLUMNS:
            out[STAT_COLUMNS[stat_id]] = value
    return out


def starters_per_team(config: LeagueConfig) -> dict[str, float]:
    """Starting slots per team, with flex shared across eligible positions.

    A flex slot is not owned by any one position, so attributing it wholly to
    RB (say) would understate replacement level for WR and TE. Splitting it
    evenly is a deliberate simplification: the true split depends on which
    position happens to be deepest, which is not knowable before the draft.
    """
    out: dict[str, float] = {}
    for slot, count in config.roster_slots.items():
        eligible = FLEX_ELIGIBLE.get(slot)
        if eligible:
            for position in eligible:
                out[position] = out.get(position, 0.0) + count / len(eligible)
        else:
            out[slot] = out.get(slot, 0.0) + float(count)
    return out
