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


def simulate_points(
    volume: float,
    eff_rate: float,
    td_rate: float,
    n: int = 10_000,
    seed: int | None = None,
) -> np.ndarray:
    """Sample a points distribution for one player-week.

    volume   expected opportunities (carries + targets)
    eff_rate expected yards per opportunity (a shrunk prior, not a prediction)
    td_rate  expected touchdowns per opportunity (heavily shrunk)
    """
    rng = np.random.default_rng(seed)
    opportunities = rng.poisson(max(volume, 0.0), size=n)
    # Yards per opportunity varies far more than its mean is knowable, so it is
    # sampled around the prior rather than treated as fixed.
    yards = rng.normal(eff_rate, eff_rate * 0.5, size=n) * opportunities
    yards = np.maximum(yards, 0.0)
    tds = rng.binomial(opportunities, min(max(td_rate, 0.0), 1.0))
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
