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
import warnings
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

# Yahoo stat ids `scoring_weights` has NO offensive-skill-player mapping for,
# and never will: these describe kicking (field goals, PATs) or team/
# individual DEFENSE performance (sacks, a defensive INT/fumble recovery,
# points allowed, a safety, a blocked kick, a fumble RETURNED for a
# touchdown BY the defense, a blocked-PAT return) -- categories this
# pipeline's own PROJECTABLE_POSITIONS (QB/RB/WR/TE, see projections.py)
# never touches, because this loader "scores offense from nflverse data"
# (module docstring) and K/DEF get no projection at all. Silently omitting
# these from `scoring_weights` carries no hidden gap: no caller ever expects
# a K/DEF number from this pipeline in the first place. This is exactly the
# "known-unmodellable, intentionally ignored" set `missing_scored_columns`
# needs to tell that case apart from a genuine, currently-undetected gap
# (see that function's docstring, and F7 in fix-round-1-brief.md).
KNOWN_UNMODELLABLE_STAT_IDS: frozenset[int] = frozenset({
    19, 20, 21, 22, 23,          # FG, by distance band
    29,                          # PAT made
    32, 33, 34, 35, 36, 37,      # sack, def INT, def fumble rec, def TD, safety, blocked kick
    50, 51, 52, 53, 54, 55, 56,  # points-allowed tiers
    57,                          # Fum Ret TD -- credited to the DEFENSE, not a skill player
    82,                          # XPR -- a blocked/missed PAT returned by the defense
})


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


def missing_scored_columns(config: LeagueConfig) -> list[dict]:
    """Scoring entries `scoring_weights` silently drops for a reason OTHER
    than "obviously a K/DEF stat this pipeline never projects."

    `scoring_weights` omits any stat id outside `STAT_COLUMNS` /
    `FUMBLES_LOST_STAT_ID` -- correct AND silent for every id in
    `KNOWN_UNMODELLABLE_STAT_IDS` (nobody expects a K/DEF number from this
    offense-only pipeline, so there is no promise being broken there). The
    SAME silent path was also swallowing 'Ret TD' (stat ids 15 and 49 --
    Yahoo tracks punt and kick returns separately but names both "Ret TD";
    nflverse's `special_teams_tds` doesn't split them further, so both map
    to the same column) and '2-PT' (stat id 16 -> the sum of
    `{passing,rushing,receiving}_2pt_conversions`) -- both stats an
    offensive skill player can score (a WR who returns kicks, an RB who
    converts a two-point try), and both columns nflverse actually carries.
    Those are a real, currently-unmodelled gap, not an intentional omission,
    and this is what lets a caller (see `projections.project_players`) tell
    the two cases apart and warn about only the second -- closing the "fail
    loud on unmodellable scoring" guarantee that module's own docstring
    makes, which this loader's silent drop was only half honouring.

    Returns the raw scoring entries (dicts, same shape as `config.scoring`)
    so a caller can report the real Yahoo stat id/name, not just a bare
    nflverse column name nothing in this dict-shaped scoring format has.
    """
    return [
        stat for stat in config.scoring
        if stat["statId"] not in STAT_COLUMNS
        and stat["statId"] != FUMBLES_LOST_STAT_ID
        and stat["statId"] not in KNOWN_UNMODELLABLE_STAT_IDS
    ]


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
