import json

import pytest

from tt.league import (
    load_config,
    load_config_from_dict,
    missing_scored_columns,
    scoring_weights,
    starters_per_team,
)

# Real shape of the export: scoring is a LIST keyed by Yahoo stat id, not an
# object keyed by display name. Yahoo reuses display names across categories
# (see COLLIDING_CONFIG below), so a name-keyed dict would silently collide.
CONFIG = {
    "leagueKey": "470.l.1433971", "name": "Definitely Not Bots",
    "numTeams": 4, "maxTeams": 10, "draftStatus": "predraft",
    "rosterSlots": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1},
    "scoring": [
        {"statId": 4, "name": "Pass Yds", "group": "passing", "value": 0.04},
        {"statId": 5, "name": "Pass TD", "group": "passing", "value": 4},
        {"statId": 6, "name": "Int", "group": "passing", "value": -1},
        {"statId": 9, "name": "Rush Yds", "group": "rushing", "value": 0.1},
        {"statId": 10, "name": "Rush TD", "group": "rushing", "value": 6},
        {"statId": 11, "name": "Rec", "group": "receiving", "value": 0.5},
        {"statId": 12, "name": "Rec Yds", "group": "receiving", "value": 0.1},
        {"statId": 13, "name": "Rec TD", "group": "receiving", "value": 6},
        {"statId": 18, "name": "Fum Lost", "group": "fumbles", "value": -2},
    ],
}

# statId 6 is the passing-interceptions penalty; statId 33 is the DEF turnover
# bonus. Yahoo names both "Int". This is the exact collision that motivated
# keying scoring by stat id: a name-keyed dict would let +2 clobber -1 and
# turn every thrown interception into a QB point gain.
COLLIDING_CONFIG = {
    **CONFIG,
    "scoring": [
        *CONFIG["scoring"],
        {"statId": 33, "name": "Int", "group": "def_turnovers", "value": 2},
    ],
}


def test_load_config_reads_the_exported_file(tmp_path):
    p = tmp_path / "league.json"
    p.write_text(json.dumps(CONFIG))
    cfg = load_config(p)
    assert cfg.num_teams == 4
    assert cfg.roster_slots["RB"] == 2


def test_scoring_weights_map_stat_ids_to_nflverse_columns():
    # Yahoo's export is keyed by stat id, not display name; scoring_weights
    # must map that id to the nflverse column, not the (colliding) name.
    w = scoring_weights(load_config_from_dict(CONFIG))
    assert w["receptions"] == 0.5
    assert w["rushing_yards"] == 0.1
    assert w["passing_tds"] == 4
    assert w["passing_interceptions"] == -1


def test_scoring_weights_keeps_passing_interception_penalty_despite_def_int_collision():
    # Regression guard for the bug that motivated keying by stat id: a config
    # containing both statId 6 (-1, QB penalty) and statId 33 (+2, DEF bonus)
    # must resolve to the QB penalty, and +2 must not leak in anywhere.
    w = scoring_weights(load_config_from_dict(COLLIDING_CONFIG))
    assert w["passing_interceptions"] == -1
    assert 2 not in w.values()


def test_scoring_weights_maps_fum_lost_to_all_three_nflverse_fumble_columns():
    # Yahoo has one combined "fumbles lost" stat; nflverse splits it by how the
    # ball was lost (rush/reception/sack). Those three are mutually exclusive
    # per play, so applying -2 to each is arithmetically equivalent to -2 per
    # fumble lost -- not a triple penalty.
    w = scoring_weights(load_config_from_dict(CONFIG))
    assert w["rushing_fumbles_lost"] == -2
    assert w["receiving_fumbles_lost"] == -2
    assert w["sack_fumbles_lost"] == -2


def test_scoring_weights_matches_half_ppr_fallback_for_this_league():
    # Sanity check: the derived weights should agree with the hardcoded
    # HALF_PPR fallback in scoring.py for this specific league's config.
    from tt.scoring import HALF_PPR

    w = scoring_weights(load_config_from_dict(CONFIG))
    for stat, weight in w.items():
        assert HALF_PPR[stat] == weight


def test_scoring_weights_omits_stats_with_no_nflverse_mapping():
    # Defensive stats, kicking and return TDs have no offensive-skill-player
    # column in nflverse; filtering them is the consumer's job, not the
    # loader's, since the export deliberately includes everything Yahoo scores.
    raw = {
        **CONFIG,
        "scoring": [
            *CONFIG["scoring"],
            {"statId": 19, "name": "FG 0-19", "group": "fgs", "value": 3},
            {"statId": 50, "name": "Pts Allow 0", "group": "pts_allow", "value": 10},
        ],
    }
    w = scoring_weights(load_config_from_dict(raw))
    assert all(v not in (3, 10) for v in w.values())


def test_missing_scored_columns_flags_ret_td_and_two_point_conversion():
    # F7: statId 15/49 (Ret TD -> nflverse special_teams_tds) and statId 16
    # (2-PT -> the *_2pt_conversions columns) are stats an offensive skill
    # player can score, and nflverse has columns for both, but
    # scoring_weights has no entry for either -- these must be reported as
    # a real, currently-unmodelled gap.
    raw = {
        **CONFIG,
        "scoring": [
            *CONFIG["scoring"],
            {"statId": 15, "name": "Ret TD", "group": "return", "value": 6},
            {"statId": 49, "name": "Ret TD", "group": "return", "value": 6},
            {"statId": 16, "name": "2-PT", "group": "misc", "value": 2},
        ],
    }
    missing = missing_scored_columns(load_config_from_dict(raw))
    missing_ids = {stat["statId"] for stat in missing}
    assert {15, 49, 16} <= missing_ids


def test_missing_scored_columns_does_not_flag_known_unmodellable_k_def_stats():
    # FG/PAT/Sack/Pts-Allow etc. are intentionally, silently omitted --
    # nobody expects a K/DEF number from this offense-only pipeline, so
    # flagging them would just be noise on every real league.
    raw = {
        **CONFIG,
        "scoring": [
            *CONFIG["scoring"],
            {"statId": 19, "name": "FG 0-19", "group": "fgs", "value": 3},
            {"statId": 50, "name": "Pts Allow 0", "group": "pts_allow", "value": 10},
        ],
    }
    missing = missing_scored_columns(load_config_from_dict(raw))
    assert missing == []


def test_missing_scored_columns_is_empty_for_a_fully_mapped_config():
    assert missing_scored_columns(load_config_from_dict(CONFIG)) == []


def test_starters_per_team_splits_flex_across_eligible_positions():
    # A W/R/T slot is filled by an RB, WR or TE. Attributing it wholly to one
    # position would misstate replacement level for all three.
    s = starters_per_team(load_config_from_dict(CONFIG))
    assert s["QB"] == 1.0
    # 2 RB + a share of the single flex
    assert 2.0 < s["RB"] < 3.0
    assert 2.0 < s["WR"] < 3.0
    assert pytest.approx(s["RB"] + s["WR"] + s["TE"], rel=1e-6) == 2 + 2 + 1 + 1


def test_starters_per_team_leaves_non_flex_positions_untouched():
    s = starters_per_team(load_config_from_dict(CONFIG))
    assert s["K"] == 1.0
    assert s["DEF"] == 1.0


def test_starters_per_team_against_the_real_league_export():
    # Sanity check against the actual exported file, gitignored so not read
    # directly by name -- but this path is stable in the repo layout.
    from pathlib import Path

    league_json = Path(__file__).parent.parent / "data" / "league.json"
    if not league_json.exists():
        pytest.skip("analytics/data/league.json not present in this environment")
    cfg = load_config(league_json)
    s = starters_per_team(cfg)
    assert s["QB"] == 1.0
    assert s["K"] == 1.0
    assert s["DEF"] == 1.0
    assert 2.0 < s["RB"] < 3.0
    assert 2.0 < s["WR"] < 3.0
