"""Monte Carlo composition of a points distribution.

Composing by simulation rather than multiplying means is what makes the playoff
optimiser possible: it needs P(my total > their total), which requires a
distribution. It also propagates efficiency's randomness (spec §2.1 measures
yards-per-carry at r=0.016) into honest width instead of a falsely precise mean.
"""
from __future__ import annotations

import numpy as np

YARDS_POINT = 0.1   # 1 point per 10 yards
TD_POINTS = 6.0


def simulate_components(
    volume: float,
    eff_rate: float,
    td_rate: float,
    n: int = 10_000,
    seed: int | None = None,
    yards_cv: float = 0.9,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Sample (opportunities, yards, touchdowns) for one player-week.

    Returned separately rather than fused into points, because scoring is
    league-specific: a half-PPR and a full-PPR league weight the same
    physical performance differently, and receptions are not derivable from
    a points total. Callers apply their own weights. Returning the shared
    `opportunities` draw explicitly (rather than making a caller reverse-
    engineer it, e.g. by re-seeding to reproduce the first internal draw) is
    also what lets a caller derive a correlated related count -- receptions
    from targets, interceptions from attempts -- that this function has no
    concept of itself, from the SAME per-sample opportunity count feeding
    yards and touchdowns.

    volume   expected opportunities (carries, targets, or attempts)
    eff_rate expected yards per opportunity (a shrunk prior, not a
             prediction). Must be non-negative -- it becomes a Gamma scale
             parameter, which cannot be negative; callers are responsible
             for clamping a shrunk rate that could go negative (e.g. a
             player with net negative rushing yards) before it reaches here,
             since this function has no domain knowledge of which stat it's
             being handed.
    td_rate  expected touchdowns per opportunity (heavily shrunk)
    yards_cv coefficient of variation of yards on a SINGLE opportunity (one
             carry or target). Yards on any one touch are highly variable --
             a stuffed run next to a broken 60-yarder -- so this defaults near
             1.0. Opportunities are summed as i.i.d. draws at this per-touch
             CV rather than scaling one week-level draw, which is what makes
             a workhorse's weekly total proportionally steadier than a
             backup's. The mean stays exactly eff_rate regardless of yards_cv
             -- this parameter only trades spread, never location.
    """
    rng = np.random.default_rng(seed)
    opportunities = rng.poisson(max(volume, 0.0), size=n)

    # Yards are the sum of `opportunities` i.i.d. per-touch draws rather than
    # a single week-level efficiency draw scaled by volume. The sum of k
    # i.i.d. Gamma(shape, scale) variables is itself Gamma(k*shape, scale), so
    # drawing directly from that summed distribution is exact and far cheaper
    # than looping per opportunity. Variance grows with opportunities (not
    # opportunities squared), so relative spread correctly narrows as volume
    # rises -- and Gamma's support is non-negative, so there is no clipping
    # and no artificial point mass at zero from truncation.
    shape_per_opp = 1.0 / (yards_cv ** 2)
    scale = eff_rate / shape_per_opp
    total_shape = np.maximum(opportunities * shape_per_opp, 1e-12)
    yards = np.where(opportunities > 0, rng.gamma(total_shape, scale, size=n), 0.0)

    tds = rng.binomial(opportunities, min(max(td_rate, 0.0), 1.0))
    return opportunities, yards, tds


def simulate_points(
    volume: float,
    eff_rate: float,
    td_rate: float,
    n: int = 10_000,
    seed: int | None = None,
    yards_cv: float = 0.9,
) -> np.ndarray:
    """Sample a points distribution for one player-week, at this module's own
    fixed conversion (YARDS_POINT, TD_POINTS). A thin wrapper over
    `simulate_components` -- see its docstring for the parameters, and for
    why the components are exposed separately for callers whose scoring
    (a league's actual per-yard/per-touchdown/per-reception weights) isn't
    this module's fixed constants.
    """
    _, yards, tds = simulate_components(volume, eff_rate, td_rate, n=n, seed=seed, yards_cv=yards_cv)
    return yards * YARDS_POINT + tds * TD_POINTS


def summarise(samples: np.ndarray) -> dict[str, float]:
    """Mean, spread and the quantiles the decision layer needs.

    p90 is the ceiling an underdog should chase; p10 is the floor a favourite
    should protect.
    """
    return {
        "mean": round(float(np.mean(samples)), 2),
        "sd": round(float(np.std(samples)), 2),
        "p10": round(float(np.percentile(samples, 10)), 2),
        "p50": round(float(np.percentile(samples, 50)), 2),
        "p90": round(float(np.percentile(samples, 90)), 2),
    }
