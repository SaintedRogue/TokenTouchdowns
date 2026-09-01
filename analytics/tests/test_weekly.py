"""Per-week projections: the same volume/shrunk-rate/compose pipeline as the
season model, run at PER-GAME volume, plus the one variance source a season
total does not have (a missed game scores exactly zero).

These tests assert LEVELS, not directions. "Weekly spread is wider than
seasonal spread" is true of a model that is wrong by a factor of four, which
is exactly the failure this module exists to fix -- so every spread assertion
below pins a magnitude (sqrt(17), a CV band, the law of total variance) and
the real-data calibration test pins the ratio against observed 2023-2025
weekly scoring.
"""
import math
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tt.league import load_config_from_dict
from tt.projections import (
    SEASON_LENGTH,
    NoProjectableDataError,
    project_players,
    season_volume,
)
from tt.weekly import (
    _STRATA,
    _gamma_strata,
    for_lineup,
    project_week,
    stream_dispersion,
    volume_dispersion,
)

# Same league shape the season-projection tests use (see tt.league's module
# docstring for why scoring is a LIST keyed by Yahoo stat id).
CONFIG = {
    "leagueKey": "470.l.1433971", "name": "Test League",
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
CONFIG_OBJ = load_config_from_dict(CONFIG)

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
REAL_SEASONS = (2022, 2023, 2024)
HOLDOUT_SEASON = 2025
_needs_real_data = pytest.mark.skipif(
    not all(
        (DATA_DIR / f"stats_player_week_{s}.parquet").exists()
        for s in REAL_SEASONS + (HOLDOUT_SEASON,)
    ),
    reason="nflverse parquet not in analytics/data (gitignored); run `tt ingest` first",
)


def history(weeks=17, seasons=(2024, 2025)):
    """A workhorse RB and a target-hog WR, both playing every week."""
    rows = []
    for season in seasons:
        for week in range(1, weeks + 1):
            rows.append({"player_id": "A", "season": season, "week": week,
                         "position": "RB", "carries": 18, "targets": 3,
                         "receptions": 2, "rushing_yards": 80, "receiving_yards": 15,
                         "rushing_tds": 0.5, "receiving_tds": 0.0})
            rows.append({"player_id": "B", "season": season, "week": week,
                         "position": "WR", "carries": 0, "targets": 9,
                         "receptions": 6, "rushing_yards": 0, "receiving_yards": 75,
                         "rushing_tds": 0.0, "receiving_tds": 0.5})
    return pd.DataFrame(rows)


def availability_history():
    """Two RBs with IDENTICAL per-game usage; one plays every week, the other
    only half of them. Everything conditional on playing must match; only
    p_active and the marginal columns may differ.
    """
    rows = []
    for season in (2024, 2025):
        for week in range(1, 18):
            rows.append({"player_id": "IRON", "season": season, "week": week,
                         "position": "RB", "carries": 15, "targets": 3,
                         "receptions": 2, "rushing_yards": 65, "receiving_yards": 15,
                         "rushing_tds": 0.4, "receiving_tds": 0.0})
            if week % 2 == 0:
                rows.append({"player_id": "GLASS", "season": season, "week": week,
                             "position": "RB", "carries": 15, "targets": 3,
                             "receptions": 2, "rushing_yards": 65, "receiving_yards": 15,
                             "rushing_tds": 0.4, "receiving_tds": 0.0})
    return pd.DataFrame(rows)


def lumpy_volume_history():
    """A back whose weekly carries alternate 0 and 20 -- same 10/game mean as a
    steady 10-carry back, but hugely over-dispersed relative to Poisson.
    """
    rows = []
    for season in (2024, 2025):
        for week in range(1, 17):
            lumpy = 20 if week % 2 == 0 else 0
            rows.append({"player_id": "LUMPY", "season": season, "week": week,
                         "position": "RB", "carries": lumpy, "targets": 0,
                         "receptions": 0, "rushing_yards": 4.5 * lumpy,
                         "receiving_yards": 0, "rushing_tds": 0.05 * lumpy,
                         "receiving_tds": 0.0})
    return pd.DataFrame(rows)


def steady_volume_history():
    """The Poisson-shaped control for `lumpy_volume_history`: same 10 carries
    per game and same per-carry rates, but delivered every week.
    """
    rows = []
    for season in (2024, 2025):
        for week in range(1, 17):
            rows.append({"player_id": "LUMPY", "season": season, "week": week,
                         "position": "RB", "carries": 10, "targets": 0,
                         "receptions": 0, "rushing_yards": 45.0,
                         "receiving_yards": 0, "rushing_tds": 0.5,
                         "receiving_tds": 0.0})
    return pd.DataFrame(rows)


def _real_history(seasons):
    frames = [
        pd.read_parquet(DATA_DIR / f"stats_player_week_{season}.parquet")
        for season in seasons
    ]
    return pd.concat(frames, ignore_index=True)


# --------------------------------------------------------------------------
# Shape and reproducibility
# --------------------------------------------------------------------------

def test_project_week_returns_the_documented_per_week_columns():
    out = project_week(history(), CONFIG_OBJ, seasons=(2024, 2025), n=2000, seed=1)
    required = {
        "player_id", "name", "position", "exp_points", "sd",
        "p10", "p50", "p90", "p_active",
    }
    assert required <= set(out.columns)
    assert set(out["player_id"]) == {"A", "B"}
    assert (out["sd"] > 0).all()
    assert (out["p10"] <= out["p50"]).all()
    assert (out["p50"] <= out["p90"]).all()


def test_project_week_is_reproducible_for_a_fixed_seed():
    first = project_week(history(), CONFIG_OBJ, seasons=(2024, 2025), n=2000, seed=11)
    second = project_week(history(), CONFIG_OBJ, seasons=(2024, 2025), n=2000, seed=11)
    pd.testing.assert_frame_equal(first, second)


def test_project_week_only_returns_projectable_positions():
    frame = history()
    kicker = frame[frame["player_id"] == "A"].copy()
    kicker["player_id"] = "K1"
    kicker["position"] = "K"
    out = project_week(
        pd.concat([frame, kicker], ignore_index=True),
        CONFIG_OBJ, seasons=(2024, 2025), n=1000, seed=3,
    )
    assert "K1" not in set(out["player_id"])


# --------------------------------------------------------------------------
# The mean: a week is a season divided by expected games, not by 17
# --------------------------------------------------------------------------

def test_weekly_mean_times_expected_games_reproduces_the_season_projection():
    """`exp_points` is CONDITIONAL on playing, so exp_points * proj_games must
    land on the season model's own total -- not merely be "smaller".
    """
    seasons = (2024, 2025)
    week = project_week(history(), CONFIG_OBJ, seasons=seasons, n=20000, seed=5)
    season = project_players(history(), CONFIG_OBJ, seasons=seasons, n=20000, seed=5)
    merged = week.drop(columns=["proj_games"]).merge(season, on="player_id")
    implied = merged["proj_points"] / merged["proj_games"]
    ratio = merged["exp_points"] / implied
    assert len(merged) == 2
    assert ((ratio - 1.0).abs() < 0.03).all(), dict(zip(merged["player_id"], ratio))


def test_poisson_mode_reproduces_project_players_at_one_game_exactly():
    """With volume over-dispersion switched off, this module IS the season
    model run at games=1: same shrinkage, same streams, same scoring, same
    per-player seeds. Pinning exact equality (not a tolerance) is what stops
    the two point formulas from silently drifting apart, since
    `projections.py` cannot be imported-and-called for the composed loop.
    """
    seasons = (2024, 2025)
    week = project_week(
        history(), CONFIG_OBJ, seasons=seasons, n=4000, seed=9, volume_dispersion=1.0,
    ).set_index("player_id")
    season = project_players(
        history(), CONFIG_OBJ, seasons=seasons, games=1, n=4000, seed=9,
    ).set_index("player_id")
    for column, season_column in (
        ("exp_points", "proj_points"), ("sd", "sd"),
        ("p10", "p10"), ("p50", "p50"), ("p90", "p90"),
    ):
        assert week[column].to_dict() == season[season_column].to_dict(), column


# --------------------------------------------------------------------------
# The spread: sqrt(17), not 17
# --------------------------------------------------------------------------

def test_weekly_sd_is_the_sqrt_of_seventeen_scaling_not_a_naive_divide_by_seventeen():
    """A season total is a sum of ~17 near-independent weeks, so weekly sd is
    season sd / sqrt(17) ~= 0.24 * season sd -- roughly FOUR TIMES the
    0.059 * season sd a naive division by 17 would give. This test fails for
    both errors: dividing by 17, and forgetting to scale at all.
    """
    seasons = (2024, 2025)
    week = project_week(
        history(), CONFIG_OBJ, seasons=seasons, n=20000, seed=4, volume_dispersion=1.0,
    ).set_index("player_id")
    season = project_players(
        history(), CONFIG_OBJ, seasons=seasons, n=20000, seed=4,
    ).set_index("player_id")
    expected = 1.0 / math.sqrt(17.0)
    for player_id in ("A", "B"):
        games = season.loc[player_id, "proj_games"]
        assert games > 15.0, "fixture assumes near-full-season availability"
        ratio = week.loc[player_id, "sd"] / season.loc[player_id, "sd"]
        assert 0.85 * expected < ratio < 1.20 * expected, (player_id, ratio, expected)


def test_weekly_coefficient_of_variation_lands_near_a_half_not_near_a_tenth():
    """The whole reason this module exists: the season model's CV at these
    scoring levels is ~0.11-0.15, far too tight for any variance-aware
    decision to have anything to trade. A single week must come out several
    times wider, in the 0.35-0.9 band a real skill player occupies.
    """
    seasons = (2024, 2025)
    week = project_week(history(), CONFIG_OBJ, seasons=seasons, n=20000, seed=6).set_index("player_id")
    season = project_players(history(), CONFIG_OBJ, seasons=seasons, n=20000, seed=6).set_index("player_id")
    for player_id in ("A", "B"):
        season_cv = season.loc[player_id, "sd"] / season.loc[player_id, "proj_points"]
        weekly_cv = week.loc[player_id, "sd"] / week.loc[player_id, "exp_points"]
        assert season_cv < 0.20, (player_id, season_cv)
        assert 0.35 < weekly_cv < 0.90, (player_id, weekly_cv)
        assert weekly_cv > 2.5 * season_cv, (player_id, weekly_cv, season_cv)


# --------------------------------------------------------------------------
# Volume over-dispersion: measured from history, not fitted to the answer
# --------------------------------------------------------------------------

def test_volume_dispersion_recovers_a_known_over_dispersion_from_history():
    """Alternating 0/20 carries has mean 10 and (n/(n-1))-corrected variance
    ~106.7, i.e. a variance-to-mean ratio of ~10.7 -- ten times Poisson.
    """
    measured = volume_dispersion(lumpy_volume_history(), "carries", min_games=6)
    assert 9.5 < measured["RB"] < 12.0, measured.to_dict()


def test_volume_dispersion_of_steady_usage_floors_at_poisson():
    """Perfectly steady weekly usage is UNDER-dispersed relative to Poisson
    (variance 0). A Poisson-Gamma mixture cannot represent that, so the
    estimator floors at 1.0 rather than returning a value the simulator would
    have to silently reinterpret.
    """
    measured = volume_dispersion(steady_volume_history(), "carries", min_games=6)
    assert measured["RB"] == pytest.approx(1.0)


def test_over_dispersed_volume_widens_the_weekly_sd_without_moving_the_mean():
    """The Poisson-Gamma volume mixture is a pure spread change: E[volume] is
    renormalised to the projected per-game volume exactly, so the mean must
    not move while the sd must move a lot.
    """
    frame = history()
    seasons = (2024, 2025)
    poisson = project_week(
        frame, CONFIG_OBJ, seasons=seasons, n=20000, seed=8, volume_dispersion=1.0,
    ).set_index("player_id")
    lumpy = project_week(
        frame, CONFIG_OBJ, seasons=seasons, n=20000, seed=8, volume_dispersion=3.0,
    ).set_index("player_id")
    for player_id in ("A", "B"):
        mean_ratio = lumpy.loc[player_id, "exp_points"] / poisson.loc[player_id, "exp_points"]
        sd_ratio = lumpy.loc[player_id, "sd"] / poisson.loc[player_id, "sd"]
        assert abs(mean_ratio - 1.0) < 0.03, (player_id, mean_ratio)
        assert sd_ratio > 1.15, (player_id, sd_ratio)


def test_gamma_strata_preserve_the_projected_volume_exactly():
    """Over-dispersion is a SPREAD parameter and must never move the mean.
    Equal-probability CONDITIONAL MEANS of the Gamma give that exactly; the
    quantile MIDPOINTS they replaced average up to 5% below the true mean at
    low volume, which would have leaked dispersion into `exp_points` for
    precisely the marginal, low-volume players a lineup call turns on.
    """
    for volume in (0.5, 1.0, 3.0, 6.0, 18.0):
        for dispersion in (1.2, 2.5, 3.0):
            points = _gamma_strata(volume, dispersion, _STRATA)
            assert points.mean() == pytest.approx(volume, rel=1e-9), (volume, dispersion)


def test_gamma_strata_deliver_most_of_the_requested_variance():
    """The known, documented cost of discretising the mixing distribution:
    a finite point set carries only the BETWEEN-stratum variance. Pinned as a
    band so the shortfall cannot quietly grow, and so the direction of the
    bias (toward UNDER-dispersion) stays on the record.
    """
    for volume, dispersion in ((1.0, 3.0), (3.0, 2.5), (18.0, 2.5), (6.0, 1.2)):
        points = _gamma_strata(volume, dispersion, _STRATA)
        realised = float(points.var()) / (volume * (dispersion - 1.0))
        assert 0.90 < realised <= 1.0, (volume, dispersion, realised)


def test_volume_dispersion_ignores_short_noisy_player_seasons():
    """A two-week cameo has a meaningless variance-to-mean ratio. Admitting
    it would drag a whole position's estimate (here from Poisson 1.0 to 2.5)
    on evidence worth one week.
    """
    steady = steady_volume_history()
    cameo = pd.DataFrame([
        {"player_id": "CAMEO", "season": 2025, "week": week, "position": "RB",
         "carries": carries, "targets": 0, "receptions": 0,
         "rushing_yards": 4.5 * carries, "receiving_yards": 0,
         "rushing_tds": 0.05 * carries, "receiving_tds": 0.0}
        for week, carries in ((3, 0), (4, 40))
    ])
    frame = pd.concat([steady, cameo], ignore_index=True)
    assert volume_dispersion(frame, "carries")["RB"] == pytest.approx(1.0)
    # ... and the filter is what does it: admit the cameo and it dominates.
    assert volume_dispersion(frame, "carries", min_games=2)["RB"] > 2.0


def mixed_dispersion_history():
    """An RB with wildly lumpy CARRIES and a WR with perfectly steady TARGETS,
    in one frame. A per-position estimate widens only the RB; one flat
    constant would widen both.
    """
    rows = []
    for season in (2024, 2025):
        for week in range(1, 17):
            lumpy = 24 if week % 2 == 0 else 0
            rows.append({"player_id": "LUMPYRB", "season": season, "week": week,
                         "position": "RB", "carries": lumpy, "targets": 0,
                         "receptions": 0, "rushing_yards": 4.5 * lumpy,
                         "receiving_yards": 0.0, "rushing_tds": 0.04 * lumpy,
                         "receiving_tds": 0.0})
            rows.append({"player_id": "STEADYWR", "season": season, "week": week,
                         "position": "WR", "carries": 0, "targets": 9,
                         "receptions": 6, "rushing_yards": 0.0,
                         "receiving_yards": 75.0, "rushing_tds": 0.0,
                         "receiving_tds": 0.5})
    return pd.DataFrame(rows)


def test_dispersion_is_measured_per_position_not_applied_as_one_flat_constant():
    """THE ANTI-FUDGE-FACTOR TEST. A single tuned constant applied to every
    position would satisfy every aggregate calibration check in this file
    while being exactly the fitted number the design brief forbids. Here the
    RB's carries are hugely over-dispersed and the WR's targets are perfectly
    steady, so a genuinely per-position MEASURED dispersion widens the RB a
    lot and leaves the WR alone -- a flat constant cannot do both.
    """
    frame = mixed_dispersion_history()
    measured = volume_dispersion(frame, "carries")
    assert measured["RB"] > 8.0, measured.to_dict()
    assert volume_dispersion(frame, "targets")["WR"] == pytest.approx(1.0)

    poisson = project_week(frame, CONFIG_OBJ, (2024, 2025), n=20000, seed=31,
                           volume_dispersion=1.0).set_index("player_id")
    default = project_week(frame, CONFIG_OBJ, (2024, 2025), n=20000, seed=31).set_index("player_id")

    rb_widening = default.loc["LUMPYRB", "sd"] / poisson.loc["LUMPYRB", "sd"]
    wr_widening = default.loc["STEADYWR", "sd"] / poisson.loc["STEADYWR", "sd"]
    assert rb_widening > 1.5, rb_widening
    assert 0.97 < wr_widening < 1.03, wr_widening


def test_stream_dispersion_reports_one_value_per_stream_and_position():
    measured = stream_dispersion(history())
    assert set(measured) == {"carries", "targets", "attempts"}
    for series in measured.values():
        assert (series >= 1.0).all()


# --------------------------------------------------------------------------
# p_active: the variance source a season total does not have
# --------------------------------------------------------------------------

def test_p_active_is_projected_games_over_the_season_length():
    frame = availability_history()
    week = project_week(frame, CONFIG_OBJ, seasons=(2024, 2025), n=2000, seed=2).set_index("player_id")
    volume = season_volume(frame, seasons=(2024, 2025)).set_index("player_id")
    for player_id in ("IRON", "GLASS"):
        expected = volume.loc[player_id, "proj_games"] / SEASON_LENGTH
        assert week.loc[player_id, "p_active"] == pytest.approx(expected, abs=1e-9)


def test_an_every_week_player_and_a_half_season_player_differ_only_in_availability():
    """The conditional columns are the SAME distribution for two players with
    identical per-game usage; only p_active and the marginal columns move.
    Getting this wrong in either direction (leaking availability into
    `exp_points`, or dropping it from the marginal columns) misleads a caller.
    """
    week = project_week(
        availability_history(), CONFIG_OBJ, seasons=(2024, 2025), n=20000, seed=12,
    ).set_index("player_id")
    iron, glass = week.loc["IRON"], week.loc["GLASS"]

    assert iron["p_active"] > 0.85, iron["p_active"]
    assert 0.40 < glass["p_active"] < 0.62, glass["p_active"]

    assert abs(iron["exp_points"] / glass["exp_points"] - 1.0) < 0.05
    assert abs(iron["sd"] / glass["sd"] - 1.0) < 0.10

    assert glass["exp_points_marginal"] < 0.70 * iron["exp_points_marginal"]


def test_marginal_mean_is_the_conditional_mean_scaled_by_p_active():
    week = project_week(
        availability_history(), CONFIG_OBJ, seasons=(2024, 2025), n=20000, seed=13,
    ).set_index("player_id")
    for player_id in ("IRON", "GLASS"):
        row = week.loc[player_id]
        expected = row["exp_points"] * row["p_active"]
        assert row["exp_points_marginal"] == pytest.approx(expected, rel=0.05), player_id


def test_marginal_variance_follows_the_law_of_total_variance():
    """Var(marginal) = p*sd^2 + p*(1-p)*mean^2. A model that merely scaled the
    conditional sd by p (or left it alone) fails this: for GLASS (p ~= 0.5,
    mean ~= 10) the missing p*(1-p)*mean^2 term is the DOMINANT one.
    """
    week = project_week(
        availability_history(), CONFIG_OBJ, seasons=(2024, 2025), n=40000, seed=14,
    ).set_index("player_id")
    for player_id in ("IRON", "GLASS"):
        row = week.loc[player_id]
        p, mu, sd = row["p_active"], row["exp_points"], row["sd"]
        expected = math.sqrt(p * sd**2 + p * (1.0 - p) * mu**2)
        assert row["sd_marginal"] == pytest.approx(expected, rel=0.06), (
            player_id, row["sd_marginal"], expected
        )


def test_marginal_floor_is_zero_for_a_player_who_often_misses():
    """A player who sits scores EXACTLY zero -- a discrete point mass the
    conditional distribution has no way to express. GLASS misses ~half his
    weeks, so his marginal p10 AND p50 are both flat zero, while his
    conditional p10 is a real (positive) floor.
    """
    week = project_week(
        availability_history(), CONFIG_OBJ, seasons=(2024, 2025), n=20000, seed=15,
    ).set_index("player_id")
    glass = week.loc["GLASS"]
    # The zero mass reaches the 10th percentile outright, and drags the
    # median down to the CONDITIONAL quantile (p_active - 0.5) / p_active --
    # 0.17 at GLASS's p_active of ~0.60, i.e. between his conditional p10 and
    # his conditional p50, and nowhere near either.
    assert glass["p10_marginal"] == 0.0
    assert glass["p10"] > 0.0
    assert glass["p50_marginal"] < 0.75 * glass["p50"]
    assert glass["p10"] < glass["p50_marginal"] < glass["p50"]
    assert glass["p90_marginal"] > 0.0


def rarely_available_history():
    """An every-week back and one who suits up three times a season."""
    rows = []
    for season in (2024, 2025):
        for week in range(1, 18):
            rows.append({"player_id": "IRON", "season": season, "week": week,
                         "position": "RB", "carries": 15, "targets": 3,
                         "receptions": 2, "rushing_yards": 65, "receiving_yards": 15,
                         "rushing_tds": 0.4, "receiving_tds": 0.0})
            if week in (4, 9, 14):
                rows.append({"player_id": "RARE", "season": season, "week": week,
                             "position": "RB", "carries": 15, "targets": 3,
                             "receptions": 2, "rushing_yards": 65, "receiving_yards": 15,
                             "rushing_tds": 0.4, "receiving_tds": 0.0})
    return pd.DataFrame(rows)


def test_a_mostly_unavailable_player_has_a_marginal_median_of_exactly_zero():
    """Below p_active = 0.5 the point mass at zero owns the median outright --
    the sharpest statement of why a marginal number is a different number,
    not a slightly smaller one.
    """
    week = project_week(
        rarely_available_history(), CONFIG_OBJ, seasons=(2024, 2025), n=20000, seed=19,
    ).set_index("player_id")
    rare = week.loc["RARE"]
    assert rare["p_active"] < 0.5, rare["p_active"]
    assert rare["p50_marginal"] == 0.0
    assert rare["p50"] > 0.0
    assert rare["exp_points"] == pytest.approx(week.loc["IRON", "exp_points"], rel=0.06)


def test_for_lineup_maps_the_chosen_view_onto_the_optimiser_column_names():
    """`lineup`/`playoff` read `proj_points`/`sd`. `for_lineup` makes the
    conditional-vs-marginal choice EXPLICIT at the call site instead of
    leaving a caller to guess which of two defensible numbers they got.
    """
    week = project_week(
        availability_history(), CONFIG_OBJ, seasons=(2024, 2025), n=8000, seed=16,
    )
    known_active = for_lineup(week).set_index("player_id")
    uncertain = for_lineup(week, marginal=True).set_index("player_id")
    indexed = week.set_index("player_id")

    assert known_active.loc["GLASS", "proj_points"] == indexed.loc["GLASS", "exp_points"]
    assert known_active.loc["GLASS", "sd"] == indexed.loc["GLASS", "sd"]
    assert uncertain.loc["GLASS", "proj_points"] == indexed.loc["GLASS", "exp_points_marginal"]
    assert uncertain.loc["GLASS", "sd"] == indexed.loc["GLASS", "sd_marginal"]
    assert uncertain.loc["GLASS", "proj_points"] < known_active.loc["GLASS", "proj_points"]


# --------------------------------------------------------------------------
# No lookahead
# --------------------------------------------------------------------------

def test_an_as_of_projection_cannot_see_the_week_it_is_projecting_or_later():
    """Truncating the future must not change a single number -- the same
    leakage property `features.prior_weeks` guarantees and the season model's
    own leakage test asserts.
    """
    full = history(weeks=17, seasons=(2024, 2025))
    truncated = full[~((full["season"] == 2025) & (full["week"] >= 9))]
    kwargs = dict(
        config=CONFIG_OBJ, seasons=(2024, 2025), n=4000, seed=21,
        as_of_season=2025, as_of_week=9,
    )
    pd.testing.assert_frame_equal(project_week(full, **kwargs), project_week(truncated, **kwargs))


def test_an_as_of_projection_does_use_earlier_weeks_of_the_same_season():
    """The counterpart to the leakage test: a point-in-time filter that threw
    away ALL same-season data would also pass a no-lookahead check, so pin
    that in-season weeks genuinely reach the projection.
    """
    frame = history(weeks=17, seasons=(2024, 2025))
    hot = frame.copy()
    in_season = (hot["season"] == 2025) & (hot["week"] < 9) & (hot["player_id"] == "A")
    hot.loc[in_season, "carries"] = 30

    base = project_week(frame, CONFIG_OBJ, (2024, 2025), n=8000, seed=22,
                        as_of_season=2025, as_of_week=9).set_index("player_id")
    boosted = project_week(hot, CONFIG_OBJ, (2024, 2025), n=8000, seed=22,
                           as_of_season=2025, as_of_week=9).set_index("player_id")
    assert boosted.loc["A", "exp_points"] > 1.15 * base.loc["A", "exp_points"]


def test_week_one_of_the_earliest_season_has_nothing_to_project_from():
    """Routing through `prior_weeks` has a consequence a non-routing
    implementation would not have: at week 1 of the FIRST season in `history`
    there is no prior data at all, so there is no positional prior to shrink
    toward and the module says so instead of inventing one.
    """
    frame = history(weeks=17, seasons=(2024, 2025))
    with pytest.raises(NoProjectableDataError):
        project_week(frame, CONFIG_OBJ, (2024, 2025), n=500, seed=1,
                     as_of_season=2024, as_of_week=1)


# --------------------------------------------------------------------------
# CALIBRATION AGAINST REAL OBSERVED WEEKLY SCORING -- the acceptance criterion
# --------------------------------------------------------------------------

@_needs_real_data
def test_weekly_sd_matches_the_observed_weekly_spread_by_position():
    """Model sd vs the EMPIRICAL within-player weekly sd of half-PPR points in
    the held-out 2025 season, for every player with at least 8 games. This is
    the only assertion that can tell a correct weekly model from a plausible
    one; everything else above is internal consistency.
    """
    from tt.league import scoring_weights
    from tt.projections import (
        PROJECTABLE_POSITIONS, _with_required_columns, regular_season,
    )

    config = load_config_from_dict(CONFIG)
    frame = _with_required_columns(regular_season(_real_history(REAL_SEASONS + (HOLDOUT_SEASON,))))
    frame = frame[frame["position"].isin(PROJECTABLE_POSITIONS)]

    model = project_week(frame, config, seasons=REAL_SEASONS, n=4000, seed=17)

    actual = frame[frame["season"] == HOLDOUT_SEASON].copy()
    points = pd.Series(0.0, index=actual.index)
    for stat, weight in scoring_weights(config).items():
        points = points + actual[stat].fillna(0.0).astype(float) * weight
    actual = actual.assign(fantasy_points=points)
    observed = (
        actual.groupby("player_id")
        .agg(games=("fantasy_points", "size"), observed_sd=("fantasy_points", "std"))
        .reset_index()
    )
    observed = observed[observed["games"] >= 8]

    joined = model.merge(observed, on="player_id")
    assert len(joined) > 200, f"only {len(joined)} players matched"

    by_position = joined.groupby("position").agg(
        players=("sd", "size"), model_sd=("sd", "mean"), observed_sd=("observed_sd", "mean"),
    )
    by_position["ratio"] = by_position["model_sd"] / by_position["observed_sd"]
    for position in ("QB", "RB", "WR", "TE"):
        ratio = by_position.loc[position, "ratio"]
        assert 0.75 < ratio < 1.25, f"{position} weekly sd ratio {ratio:.3f}\n{by_position}"


@_needs_real_data
def test_weekly_mean_matches_the_observed_weekly_scoring_level():
    """Calibration of the LOCATION, not just the spread: an sd ratio near 1
    means nothing if the mean it is centred on is wrong.
    """
    from tt.league import scoring_weights
    from tt.projections import (
        PROJECTABLE_POSITIONS, _with_required_columns, regular_season,
    )

    config = load_config_from_dict(CONFIG)
    frame = _with_required_columns(regular_season(_real_history(REAL_SEASONS + (HOLDOUT_SEASON,))))
    frame = frame[frame["position"].isin(PROJECTABLE_POSITIONS)]
    model = project_week(frame, config, seasons=REAL_SEASONS, n=4000, seed=18)

    actual = frame[frame["season"] == HOLDOUT_SEASON].copy()
    points = pd.Series(0.0, index=actual.index)
    for stat, weight in scoring_weights(config).items():
        points = points + actual[stat].fillna(0.0).astype(float) * weight
    actual = actual.assign(fantasy_points=points)
    observed = (
        actual.groupby("player_id")
        .agg(games=("fantasy_points", "size"), observed_mean=("fantasy_points", "mean"))
        .reset_index()
    )
    observed = observed[observed["games"] >= 8]
    joined = model.merge(observed, on="player_id")

    by_position = joined.groupby("position").agg(
        model_mean=("exp_points", "mean"), observed_mean=("observed_mean", "mean"),
    )
    by_position["ratio"] = by_position["model_mean"] / by_position["observed_mean"]
    for position in ("QB", "RB", "WR", "TE"):
        ratio = by_position.loc[position, "ratio"]
        assert 0.85 < ratio < 1.20, f"{position} weekly mean ratio {ratio:.3f}\n{by_position}"
