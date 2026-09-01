# Prediction Engine Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingest nflverse history, build point-in-time-correct features, and validate a component-wise projection model against ADP and trailing-average baselines.

**Architecture:** A Python package `analytics/` using DuckDB over local parquet. Ingestion pulls nflverse releases; features are computed as-of a week with structural leakage protection; a walk-forward backtest harness is built BEFORE any model and gates every model against baselines. Node keeps the Yahoo session and CLI unchanged.

**Tech Stack:** Python 3.14 via `uv`, duckdb, pandas, numpy, scikit-learn, pyarrow, pytest.

**Spec:** `docs/prediction-engine-design.md`

## Global Constraints

- Python 3.14, environment managed by `uv` in `analytics/.venv`. Never install into system Python.
- Tests use `pytest`. Run from `analytics/`: `.venv/bin/python -m pytest -q`
- TDD is mandatory: write the failing test, run it, watch it fail for the RIGHT reason, then implement.
- **Point-in-time correctness is structural, not conventional.** Every feature function takes an `as_of_week` and must be provably unable to read rows at or after it.
- Data cache lives in `analytics/data/` and is gitignored. NEVER commit parquet or CSV data larger than a test fixture.
- The existing Node code (`src/`, `bin/`, `test/`) must not be modified in Phase 1. The suite there stays at 172 passing.
- Half-PPR scoring: 0.5/reception, 0.1/rushing+receiving yard, 6/rushing+receiving TD, 0.04/passing yard, 4/passing TD, -2/fumble lost, -1/interception.
- nflverse asset URLs are pinned by name: `https://github.com/nflverse/nflverse-data/releases/download/<dataset>/<file>`

---

## File Structure

| File | Responsibility |
|---|---|
| `analytics/pyproject.toml` | Package metadata and dependencies |
| `analytics/src/tt/ingest.py` | nflverse release -> local parquet, idempotent |
| `analytics/src/tt/store.py` | DuckDB connection + parquet registration |
| `analytics/src/tt/scoring.py` | Component stats -> half-PPR points |
| `analytics/src/tt/features.py` | Point-in-time rolling features |
| `analytics/src/tt/backtest.py` | Walk-forward splits, baselines, metrics |
| `analytics/src/tt/models/volume.py` | Predict carries/targets |
| `analytics/src/tt/models/compose.py` | Efficiency + TD priors, Monte Carlo composition |
| `analytics/tests/` | pytest suite mirroring the above |

---

### Task 1: Package scaffold

**Files:**
- Create: `analytics/pyproject.toml`, `analytics/src/tt/__init__.py`, `analytics/tests/test_smoke.py`, `analytics/.gitignore`
- Modify: none

**Interfaces:**
- Consumes: nothing.
- Produces: an importable `tt` package and a working `pytest` invocation.

- [ ] **Step 1: Write the failing test**

```python
# analytics/tests/test_smoke.py
from tt import __version__


def test_package_imports_and_declares_a_version():
    assert isinstance(__version__, str)
    assert __version__
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd analytics && uv run --python 3.14 python -m pytest tests/test_smoke.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'tt'`

- [ ] **Step 3: Create the package**

```toml
# analytics/pyproject.toml
[project]
name = "tt-analytics"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "duckdb>=1.1",
    "pandas>=2.2",
    "numpy>=2.0",
    "pyarrow>=17",
    "scikit-learn>=1.5",
    "scipy>=1.14",
]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/tt"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
```

```python
# analytics/src/tt/__init__.py
"""Fantasy football projection pipeline.

Component-wise by design: volume is predicted, efficiency is regressed, and
points are composed. See docs/prediction-engine-design.md for the measurements
that determined this.
"""

__version__ = "0.1.0"
```

```
# analytics/.gitignore
.venv/
data/
__pycache__/
*.pyc
.pytest_cache/
```

- [ ] **Step 4: Create the venv and confirm the test passes**

```bash
cd analytics
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -q
```
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add analytics/pyproject.toml analytics/src analytics/tests analytics/.gitignore
git commit -m "feat(analytics): python package scaffold"
```

---

### Task 2: Half-PPR scoring

Scoring comes before ingestion because it is pure, fully testable, and every later task depends on the target variable being right.

**Files:**
- Create: `analytics/src/tt/scoring.py`, `analytics/tests/test_scoring.py`

**Interfaces:**
- Produces: `HALF_PPR: dict[str, float]`, `score_row(row: Mapping) -> float`, `score_frame(df: DataFrame) -> Series`.

- [ ] **Step 1: Write the failing test**

```python
# analytics/tests/test_scoring.py
import pandas as pd
from tt.scoring import HALF_PPR, score_row, score_frame


def test_half_ppr_gives_half_a_point_per_reception():
    assert HALF_PPR["receptions"] == 0.5


def test_score_row_composes_receiving_line():
    # 6 catches, 80 yards, 1 TD = 3.0 + 8.0 + 6.0
    row = {"receptions": 6, "receiving_yards": 80, "receiving_tds": 1}
    assert score_row(row) == 17.0


def test_score_row_composes_rushing_line():
    # 20 carries for 100 yards and 2 TDs = 10.0 + 12.0 (carries are not scored)
    row = {"carries": 20, "rushing_yards": 100, "rushing_tds": 2}
    assert score_row(row) == 22.0


def test_score_row_applies_negative_scoring():
    row = {"passing_yards": 250, "passing_tds": 2, "passing_interceptions": 1}
    # 10.0 + 8.0 - 1.0
    assert score_row(row) == 17.0


def test_score_row_treats_missing_and_null_stats_as_zero():
    assert score_row({}) == 0.0
    assert score_row({"receiving_yards": None, "receptions": 2}) == 1.0


def test_score_frame_scores_every_row():
    df = pd.DataFrame([
        {"receptions": 6, "receiving_yards": 80, "receiving_tds": 1},
        {"carries": 20, "rushing_yards": 100, "rushing_tds": 2},
    ])
    assert list(score_frame(df)) == [17.0, 22.0]


def test_score_frame_ignores_columns_that_are_not_scored():
    df = pd.DataFrame([{"receptions": 2, "target_share": 0.9, "player_name": "X"}])
    assert list(score_frame(df)) == [1.0]
```

- [ ] **Step 2: Run and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_scoring.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'tt.scoring'`

- [ ] **Step 3: Implement**

```python
# analytics/src/tt/scoring.py
"""Half-PPR scoring.

nflverse ships `fantasy_points` (standard) and `fantasy_points_ppr` (full PPR).
Neither is this league's scoring, so points are composed from components. That
constraint happens to agree with the modelling architecture: components are what
we predict, so points are always a derived quantity anyway.
"""
from collections.abc import Mapping

import pandas as pd

# Verified against the committed league-settings capture: RECEPTION modifier 0.5.
HALF_PPR: dict[str, float] = {
    "receptions": 0.5,
    "receiving_yards": 0.1,
    "receiving_tds": 6.0,
    "carries": 0.0,          # opportunities are not scored, only their results
    "rushing_yards": 0.1,
    "rushing_tds": 6.0,
    "passing_yards": 0.04,
    "passing_tds": 4.0,
    "passing_interceptions": -1.0,
    "rushing_fumbles_lost": -2.0,
    "receiving_fumbles_lost": -2.0,
    "sack_fumbles_lost": -2.0,
}


def score_row(row: Mapping, weights: Mapping[str, float] = HALF_PPR) -> float:
    """Points for one player-week. Absent or null stats count as zero."""
    total = 0.0
    for stat, weight in weights.items():
        value = row.get(stat)
        if value is None or (isinstance(value, float) and pd.isna(value)):
            continue
        total += float(value) * weight
    return round(total, 2)


def score_frame(df: pd.DataFrame, weights: Mapping[str, float] = HALF_PPR) -> pd.Series:
    """Vectorised `score_row` over a frame. Columns not in `weights` are ignored."""
    total = pd.Series(0.0, index=df.index)
    for stat, weight in weights.items():
        if stat in df.columns:
            total = total + df[stat].fillna(0).astype(float) * weight
    return total.round(2)
```

- [ ] **Step 4: Run and watch it pass**

Run: `.venv/bin/python -m pytest tests/test_scoring.py -q`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add analytics/src/tt/scoring.py analytics/tests/test_scoring.py
git commit -m "feat(analytics): half-PPR scoring composed from components"
```

---

### Task 3: nflverse ingestion

**Files:**
- Create: `analytics/src/tt/ingest.py`, `analytics/tests/test_ingest.py`

**Interfaces:**
- Produces: `nflverse_url(dataset, filename) -> str`, `season_asset(dataset, season) -> str`, `fetch_season(dataset, season, data_dir, fetch=...) -> Path`, `load_seasons(dataset, seasons, data_dir) -> DataFrame`.

- [ ] **Step 1: Write the failing test**

```python
# analytics/tests/test_ingest.py
import pandas as pd
import pytest
from tt.ingest import nflverse_url, season_asset, fetch_season


def test_nflverse_url_pins_the_release_asset_path():
    url = nflverse_url("stats_player", "stats_player_week_2025.parquet")
    assert url == (
        "https://github.com/nflverse/nflverse-data/releases/download/"
        "stats_player/stats_player_week_2025.parquet"
    )


def test_season_asset_names_the_per_season_file():
    assert season_asset("stats_player", 2025) == "stats_player_week_2025.parquet"


def test_fetch_season_writes_the_payload_and_returns_its_path(tmp_path):
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return b"PARQUET-BYTES"

    path = fetch_season("stats_player", 2025, tmp_path, fetch=fake_fetch)
    assert path.exists()
    assert path.read_bytes() == b"PARQUET-BYTES"
    assert len(calls) == 1


def test_fetch_season_is_idempotent_and_does_not_refetch(tmp_path):
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return b"PARQUET-BYTES"

    fetch_season("stats_player", 2025, tmp_path, fetch=fake_fetch)
    fetch_season("stats_player", 2025, tmp_path, fetch=fake_fetch)
    assert len(calls) == 1, "a cached season must not be refetched"


def test_fetch_season_refetches_when_forced(tmp_path):
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return b"X"

    fetch_season("stats_player", 2025, tmp_path, fetch=fake_fetch)
    fetch_season("stats_player", 2025, tmp_path, fetch=fake_fetch, force=True)
    assert len(calls) == 2


def test_fetch_season_does_not_leave_a_partial_file_on_failure(tmp_path):
    def boom(url):
        raise RuntimeError("network down")

    with pytest.raises(RuntimeError):
        fetch_season("stats_player", 2025, tmp_path, fetch=boom)
    # A half-written cache would be silently trusted on the next run.
    assert list(tmp_path.glob("*")) == []
```

- [ ] **Step 2: Run and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_ingest.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'tt.ingest'`

- [ ] **Step 3: Implement**

```python
# analytics/src/tt/ingest.py
"""nflverse release ingestion.

Assets are pinned by name rather than discovered, so a layout change fails
loudly instead of silently fetching the wrong file.
"""
from __future__ import annotations

import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path

import pandas as pd

BASE = "https://github.com/nflverse/nflverse-data/releases/download"

# dataset -> filename template. Extend deliberately, not dynamically.
ASSETS: dict[str, str] = {
    "stats_player": "stats_player_week_{season}.parquet",
    "snap_counts": "snap_counts_{season}.parquet",
    "weekly_rosters": "roster_weekly_{season}.parquet",
}


def nflverse_url(dataset: str, filename: str) -> str:
    return f"{BASE}/{dataset}/{filename}"


def season_asset(dataset: str, season: int) -> str:
    try:
        return ASSETS[dataset].format(season=season)
    except KeyError:
        raise ValueError(
            f"unknown dataset {dataset!r}; known: {sorted(ASSETS)}"
        ) from None


def _http_get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310
        return response.read()


def fetch_season(
    dataset: str,
    season: int,
    data_dir: Path,
    fetch: Callable[[str], bytes] = _http_get,
    force: bool = False,
) -> Path:
    """Download one season to `data_dir`, returning its path.

    Writes to a temporary file and renames, so an interrupted download can never
    leave a truncated file that later runs would trust.
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    filename = season_asset(dataset, season)
    target = data_dir / filename
    if target.exists() and not force:
        return target

    payload = fetch(nflverse_url(dataset, filename))
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(payload)
    tmp.replace(target)
    return target


def load_seasons(dataset: str, seasons: Iterable[int], data_dir: Path) -> pd.DataFrame:
    """Concatenate cached seasons into one frame. Fetches anything missing."""
    frames = [
        pd.read_parquet(fetch_season(dataset, season, data_dir))
        for season in seasons
    ]
    return pd.concat(frames, ignore_index=True)
```

- [ ] **Step 4: Run and watch it pass**

Run: `.venv/bin/python -m pytest tests/test_ingest.py -q`
Expected: 6 passed.

- [ ] **Step 5: Fetch real data and confirm the shape**

```bash
.venv/bin/python -c "
from pathlib import Path
from tt.ingest import fetch_season
import pandas as pd
p = fetch_season('stats_player', 2025, Path('data'))
df = pd.read_parquet(p)
print(p, df.shape)
print([c for c in ('player_id','season','week','targets','carries','opponent_team') if c in df.columns])
"
```
Expected: a parquet under `analytics/data/`, roughly (19000+, 150), and all six columns present.

- [ ] **Step 6: Commit**

```bash
git add analytics/src/tt/ingest.py analytics/tests/test_ingest.py
git commit -m "feat(analytics): nflverse season ingestion with atomic writes"
```

---

### Task 4: Point-in-time feature engineering

This is the highest-risk task in the plan. A leaky feature makes every downstream number optimistic and is invisible without a structural guard.

**Files:**
- Create: `analytics/src/tt/features.py`, `analytics/tests/test_features.py`

**Interfaces:**
- Produces: `PointInTimeError`, `prior_weeks(df, as_of_season, as_of_week) -> DataFrame`, `rolling_volume(df, as_of_season, as_of_week, windows=(3, 8)) -> DataFrame`, `shrunk_rate(numerator, denominator, prior, strength) -> float`.

- [ ] **Step 1: Write the failing test**

```python
# analytics/tests/test_features.py
import pandas as pd
import pytest
from tt.features import prior_weeks, rolling_volume, shrunk_rate


def frame():
    # One player, weeks 1-5, rising usage.
    return pd.DataFrame([
        {"player_id": "A", "season": 2025, "week": w,
         "carries": w * 2, "targets": w, "receptions": w}
        for w in range(1, 6)
    ])


def test_prior_weeks_excludes_the_as_of_week_itself():
    out = prior_weeks(frame(), 2025, 3)
    assert sorted(out["week"]) == [1, 2]


def test_prior_weeks_excludes_all_future_weeks():
    out = prior_weeks(frame(), 2025, 3)
    assert out["week"].max() < 3


def test_prior_weeks_includes_earlier_seasons():
    df = pd.concat([
        frame(),
        frame().assign(season=2024),
    ], ignore_index=True)
    out = prior_weeks(df, 2025, 2)
    assert set(out["season"]) == {2024, 2025}
    assert out[out["season"] == 2025]["week"].max() == 1


def test_rolling_volume_is_identical_whether_or_not_future_rows_exist():
    # THE leakage guard. Truncating the future must not change the answer.
    full = frame()
    truncated = full[full["week"] < 4]
    a = rolling_volume(full, 2025, 4)
    b = rolling_volume(truncated, 2025, 4)
    pd.testing.assert_frame_equal(
        a.reset_index(drop=True), b.reset_index(drop=True)
    )


def test_rolling_volume_averages_only_the_requested_window():
    # as_of week 5, window 3 -> weeks 2,3,4 -> carries 4,6,8 -> mean 6.0
    out = rolling_volume(frame(), 2025, 5, windows=(3,))
    row = out[out["player_id"] == "A"].iloc[0]
    assert row["carries_r3"] == pytest.approx(6.0)


def test_rolling_volume_returns_no_rows_for_a_player_with_no_history():
    out = rolling_volume(frame(), 2025, 1)
    assert out.empty


def test_shrunk_rate_pulls_a_small_sample_toward_the_prior():
    # 1 TD on 2 carries is 0.5, but two carries is nothing. With a 0.05 prior
    # and strength 50, the estimate must stay near the prior.
    assert shrunk_rate(1, 2, prior=0.05, strength=50) == pytest.approx(
        (1 + 0.05 * 50) / (2 + 50)
    )
    assert shrunk_rate(1, 2, prior=0.05, strength=50) < 0.10


def test_shrunk_rate_approaches_the_observed_rate_with_a_large_sample():
    assert shrunk_rate(100, 1000, prior=0.05, strength=50) == pytest.approx(
        (100 + 2.5) / 1050
    )


def test_shrunk_rate_returns_the_prior_with_no_observations():
    assert shrunk_rate(0, 0, prior=0.05, strength=50) == pytest.approx(0.05)
```

- [ ] **Step 2: Run and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_features.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'tt.features'`

- [ ] **Step 3: Implement**

```python
# analytics/src/tt/features.py
"""Point-in-time feature engineering.

Every function here takes an as-of (season, week) and may only read rows
STRICTLY BEFORE it. This is structural, not conventional: `prior_weeks` is the
only gateway to history, and the leakage test asserts that truncating the future
cannot change any feature value.
"""
from __future__ import annotations

import pandas as pd

VOLUME_COLUMNS = ("carries", "targets", "receptions")


class PointInTimeError(RuntimeError):
    """A feature tried to read data at or after its as-of week."""


def prior_weeks(df: pd.DataFrame, as_of_season: int, as_of_week: int) -> pd.DataFrame:
    """Rows strictly before (as_of_season, as_of_week).

    Earlier seasons are included in full; the current season is cut at the week
    being predicted. Nothing at or after the as-of point is ever visible.
    """
    earlier_season = df["season"] < as_of_season
    same_season_earlier_week = (df["season"] == as_of_season) & (df["week"] < as_of_week)
    return df[earlier_season | same_season_earlier_week].copy()


def rolling_volume(
    df: pd.DataFrame,
    as_of_season: int,
    as_of_week: int,
    windows: tuple[int, ...] = (3, 8),
) -> pd.DataFrame:
    """Per-player mean volume over the last N weeks before the as-of point.

    Volume is what the spec's measurements say is actually predictable
    (carries r=0.825, targets r=0.623), so it is the model's real input.
    """
    history = prior_weeks(df, as_of_season, as_of_week)
    if history.empty:
        return pd.DataFrame(columns=["player_id"])

    history = history.sort_values(["player_id", "season", "week"])
    out = history[["player_id"]].drop_duplicates().reset_index(drop=True)

    for window in windows:
        tail = (
            history.groupby("player_id", group_keys=False)
            .tail(window)
            .groupby("player_id")[list(VOLUME_COLUMNS)]
            .mean()
            .rename(columns={c: f"{c}_r{window}" for c in VOLUME_COLUMNS})
            .reset_index()
        )
        out = out.merge(tail, on="player_id", how="left")

    return out


def shrunk_rate(
    numerator: float, denominator: float, prior: float, strength: float
) -> float:
    """Empirical-Bayes shrinkage toward `prior`.

    Used for efficiency and touchdown rates, which the spec measures as
    functionally random (yards/carry r=0.016, TDs r=0.147). `strength` is the
    pseudo-count: how many observations of the prior the estimate is worth.
    """
    return (numerator + prior * strength) / (denominator + strength)
```

- [ ] **Step 4: Run and watch it pass**

Run: `.venv/bin/python -m pytest tests/test_features.py -q`
Expected: 9 passed.

- [ ] **Step 5: Prove the leakage guard actually guards**

Temporarily change `prior_weeks` to use `<=` instead of `<` for the same-season comparison. Run the suite. `test_prior_weeks_excludes_the_as_of_week_itself` and `test_rolling_volume_averages_only_the_requested_window` must FAIL. Revert and confirm they pass. Record both observations in your report — a leakage guard nobody has seen fail is not a guard.

- [ ] **Step 6: Commit**

```bash
git add analytics/src/tt/features.py analytics/tests/test_features.py
git commit -m "feat(analytics): point-in-time features with structural leakage guard"
```

---

### Task 5: Backtest harness

Built before any model, so no model can be blessed by an untested harness.

**Files:**
- Create: `analytics/src/tt/backtest.py`, `analytics/tests/test_backtest.py`

**Interfaces:**
- Produces: `walk_forward(df, start_season, start_week) -> Iterator[tuple[int, int]]`, `baseline_last_n(df, as_of_season, as_of_week, n=3) -> DataFrame`, `mae(pred, actual) -> float`, `rmse(pred, actual) -> float`, `spearman(pred, actual) -> float`, `evaluate(predictions, actuals) -> dict`.

- [ ] **Step 1: Write the failing test**

```python
# analytics/tests/test_backtest.py
import numpy as np
import pandas as pd
import pytest
from tt.backtest import walk_forward, baseline_last_n, mae, rmse, spearman, evaluate


def frame():
    rows = []
    for season in (2024, 2025):
        for week in range(1, 4):
            rows.append({"player_id": "A", "season": season, "week": week,
                         "points": 10.0 + week})
    return pd.DataFrame(rows)


def test_walk_forward_yields_folds_in_chronological_order():
    folds = list(walk_forward(frame(), start_season=2025, start_week=2))
    assert folds == [(2025, 2), (2025, 3)]


def test_walk_forward_never_yields_a_fold_before_the_start():
    folds = list(walk_forward(frame(), start_season=2025, start_week=2))
    assert all((s, w) >= (2025, 2) for s, w in folds)


def test_baseline_last_n_averages_only_prior_weeks():
    # as_of 2025 wk3, n=2 -> weeks 1,2 -> points 11,12 -> 11.5
    out = baseline_last_n(frame(), 2025, 3, n=2)
    assert out.set_index("player_id").loc["A", "pred"] == pytest.approx(11.5)


def test_mae_and_rmse_are_zero_for_a_perfect_prediction():
    a = np.array([1.0, 2.0, 3.0])
    assert mae(a, a) == 0.0
    assert rmse(a, a) == 0.0


def test_rmse_punishes_a_single_large_error_more_than_mae():
    actual = np.array([0.0, 0.0, 0.0, 0.0])
    pred = np.array([0.0, 0.0, 0.0, 8.0])
    assert rmse(pred, actual) > mae(pred, actual)


def test_spearman_is_one_for_a_perfectly_ordered_prediction():
    assert spearman(np.array([1.0, 2.0, 3.0]), np.array([10.0, 20.0, 30.0])) == pytest.approx(1.0)


def test_spearman_is_negative_one_when_the_order_is_reversed():
    assert spearman(np.array([3.0, 2.0, 1.0]), np.array([10.0, 20.0, 30.0])) == pytest.approx(-1.0)


def test_evaluate_reports_every_metric_and_the_sample_size():
    out = evaluate(np.array([1.0, 2.0, 3.0]), np.array([1.0, 2.0, 4.0]))
    assert set(out) == {"n", "mae", "rmse", "spearman"}
    assert out["n"] == 3


def test_evaluate_on_a_synthetic_series_with_a_known_answer():
    # A harness that cannot recover a known answer cannot validate a model.
    actual = np.array([2.0, 4.0, 6.0, 8.0])
    pred = actual + 1.0                      # constant bias of exactly 1.0
    out = evaluate(pred, actual)
    assert out["mae"] == pytest.approx(1.0)
    assert out["rmse"] == pytest.approx(1.0)
    assert out["spearman"] == pytest.approx(1.0)
```

- [ ] **Step 2: Run and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_backtest.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'tt.backtest'`

- [ ] **Step 3: Implement**

```python
# analytics/src/tt/backtest.py
"""Walk-forward backtesting.

Deliberately built before any model. Its baselines are the gate: a model that
cannot beat a trailing average or ADP order is worse than the data already in
hand, and should not ship.
"""
from __future__ import annotations

from collections.abc import Iterator

import numpy as np
import pandas as pd
from scipy import stats

from .features import prior_weeks


def walk_forward(
    df: pd.DataFrame, start_season: int, start_week: int
) -> Iterator[tuple[int, int]]:
    """Yield (season, week) folds in chronological order from the start point."""
    pairs = (
        df[["season", "week"]].drop_duplicates().sort_values(["season", "week"])
    )
    for season, week in pairs.itertuples(index=False):
        if (season, week) >= (start_season, start_week):
            yield int(season), int(week)


def baseline_last_n(
    df: pd.DataFrame, as_of_season: int, as_of_week: int, n: int = 3,
    value_column: str = "points",
) -> pd.DataFrame:
    """Trailing mean of the last n weeks. The baseline a model must beat."""
    history = prior_weeks(df, as_of_season, as_of_week)
    if history.empty:
        return pd.DataFrame(columns=["player_id", "pred"])
    history = history.sort_values(["player_id", "season", "week"])
    return (
        history.groupby("player_id", group_keys=False)
        .tail(n)
        .groupby("player_id")[value_column]
        .mean()
        .reset_index()
        .rename(columns={value_column: "pred"})
    )


def mae(pred: np.ndarray, actual: np.ndarray) -> float:
    return float(np.mean(np.abs(np.asarray(pred) - np.asarray(actual))))


def rmse(pred: np.ndarray, actual: np.ndarray) -> float:
    return float(np.sqrt(np.mean((np.asarray(pred) - np.asarray(actual)) ** 2)))


def spearman(pred: np.ndarray, actual: np.ndarray) -> float:
    """Rank correlation. For lineup decisions, order matters more than level."""
    return float(stats.spearmanr(pred, actual).statistic)


def evaluate(pred: np.ndarray, actual: np.ndarray) -> dict[str, float]:
    return {
        "n": int(len(pred)),
        "mae": mae(pred, actual),
        "rmse": rmse(pred, actual),
        "spearman": spearman(pred, actual),
    }
```

- [ ] **Step 4: Run and watch it pass**

Run: `.venv/bin/python -m pytest tests/test_backtest.py -q`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add analytics/src/tt/backtest.py analytics/tests/test_backtest.py
git commit -m "feat(analytics): walk-forward backtest harness with baselines"
```

---

### Task 6: Replicate the DvP finding across seasons

The spec's §9.1 names this as the harness's first job. It is also the cheapest possible proof the harness works on real data.

**Files:**
- Create: `analytics/src/tt/studies/dvp.py`, `analytics/tests/test_dvp.py`

**Interfaces:**
- Produces: `split_half_reliability(df, positions, split_week=9) -> DataFrame`.

- [ ] **Step 1: Write the failing test**

```python
# analytics/tests/test_dvp.py
import numpy as np
import pandas as pd
import pytest
from tt.studies.dvp import split_half_reliability


def synthetic(consistent: bool):
    """Defences whose points-allowed either persists across halves or does not."""
    rng = np.random.default_rng(0)
    rows = []
    for team_index in range(32):
        strength = team_index / 32.0
        for week in range(1, 19):
            first_half = week <= 9
            level = strength if (consistent or first_half) else (1.0 - strength)
            rows.append({
                "opponent_team": f"T{team_index:02d}",
                "position": "RB",
                "week": week,
                "points": level * 10 + rng.normal(0, 0.01),
            })
    return pd.DataFrame(rows)


def test_split_half_reliability_is_high_for_a_consistent_defence():
    out = split_half_reliability(synthetic(consistent=True), positions=("RB",))
    assert out.set_index("position").loc["RB", "r"] > 0.95


def test_split_half_reliability_is_negative_when_halves_invert():
    out = split_half_reliability(synthetic(consistent=False), positions=("RB",))
    assert out.set_index("position").loc["RB", "r"] < -0.95


def test_split_half_reliability_reports_the_team_count():
    out = split_half_reliability(synthetic(consistent=True), positions=("RB",))
    assert out.set_index("position").loc["RB", "teams"] == 32
```

- [ ] **Step 2: Run and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_dvp.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'tt.studies'`

- [ ] **Step 3: Implement**

```python
# analytics/src/tt/studies/__init__.py
"""One-off analyses that inform the design rather than run in production."""
```

```python
# analytics/src/tt/studies/dvp.py
"""Defence-vs-position reliability.

Spec §2.2 measured this on 2025 alone and found it close to noise, and NEGATIVE
against RB and TE. §9.1 requires replication across seasons before the finding is
treated as settled, because it is the justification for NOT building a full
opponent-adjustment subsystem.
"""
from __future__ import annotations

import pandas as pd


def split_half_reliability(
    df: pd.DataFrame,
    positions: tuple[str, ...] = ("QB", "RB", "WR", "TE"),
    split_week: int = 9,
    value_column: str = "points",
) -> pd.DataFrame:
    """Correlate each defence's points allowed in weeks 1..split_week against
    the rest of the season. A high r means the signal persists; a low or
    negative r means published points-allowed tables mostly measure schedule.
    """
    subset = df[df["position"].isin(positions)].copy()
    subset["half"] = (subset["week"] > split_week).map({False: "h1", True: "h2"})

    halves = (
        subset.groupby(["opponent_team", "position", "half"])[value_column]
        .mean()
        .reset_index()
    )
    wide = halves.pivot_table(
        index=["opponent_team", "position"], columns="half", values=value_column
    ).reset_index()
    wide = wide.dropna(subset=["h1", "h2"])

    out = []
    for position, group in wide.groupby("position"):
        out.append({
            "position": position,
            "teams": int(len(group)),
            "r": round(float(group["h1"].corr(group["h2"])), 3),
        })
    return pd.DataFrame(out).sort_values("position").reset_index(drop=True)
```

- [ ] **Step 4: Run and watch it pass**

Run: `.venv/bin/python -m pytest tests/test_dvp.py -q`
Expected: 3 passed.

- [ ] **Step 5: Run the real replication across 2015-2025**

```bash
.venv/bin/python -c "
from pathlib import Path
import pandas as pd
from tt.ingest import load_seasons
from tt.scoring import score_frame
from tt.studies.dvp import split_half_reliability

df = load_seasons('stats_player', range(2015, 2026), Path('data'))
df = df[(df['season_type']=='REG') & df['opponent_team'].notna() & (df['week']<=18)]
df['points'] = score_frame(df)
for season, group in df.groupby('season'):
    r = split_half_reliability(group)
    print(season, dict(zip(r['position'], r['r'])))
"
```
Record the output in your report. This either confirms spec §2.2 across eleven seasons or overturns it; both are valuable, and the second is more so.

- [ ] **Step 6: Commit**

```bash
git add analytics/src/tt/studies analytics/tests/test_dvp.py
git commit -m "feat(analytics): defence-vs-position split-half study"
```

---

### Task 7: Volume model and composition

**Files:**
- Create: `analytics/src/tt/models/__init__.py`, `analytics/src/tt/models/compose.py`, `analytics/tests/test_compose.py`

**Interfaces:**
- Produces: `simulate_points(volume, eff_rate, td_rate, n=10000, seed=None) -> np.ndarray`, `summarise(samples) -> dict` with keys `mean`, `p10`, `p50`, `p90`, `sd`.

- [ ] **Step 1: Write the failing test**

```python
# analytics/tests/test_compose.py
import numpy as np
import pytest
from tt.models.compose import simulate_points, summarise


def test_simulate_points_returns_the_requested_sample_count():
    s = simulate_points(volume=10.0, eff_rate=4.0, td_rate=0.05, n=500, seed=1)
    assert len(s) == 500


def test_simulate_points_is_deterministic_for_a_fixed_seed():
    a = simulate_points(volume=10.0, eff_rate=4.0, td_rate=0.05, n=200, seed=7)
    b = simulate_points(volume=10.0, eff_rate=4.0, td_rate=0.05, n=200, seed=7)
    assert np.array_equal(a, b)


def test_higher_volume_raises_the_mean():
    low = simulate_points(volume=5.0, eff_rate=4.0, td_rate=0.05, n=4000, seed=3).mean()
    high = simulate_points(volume=15.0, eff_rate=4.0, td_rate=0.05, n=4000, seed=3).mean()
    assert high > low


def test_a_higher_td_rate_widens_the_distribution():
    # Touchdowns are the lumpy component: 6 points arriving at random is the
    # dominant source of week-to-week variance.
    calm = simulate_points(volume=10.0, eff_rate=4.0, td_rate=0.01, n=4000, seed=5)
    spiky = simulate_points(volume=10.0, eff_rate=4.0, td_rate=0.30, n=4000, seed=5)
    assert spiky.std() > calm.std()


def test_summarise_reports_ordered_quantiles():
    s = simulate_points(volume=10.0, eff_rate=4.0, td_rate=0.05, n=4000, seed=11)
    out = summarise(s)
    assert out["p10"] <= out["p50"] <= out["p90"]
    assert set(out) == {"mean", "sd", "p10", "p50", "p90"}


def test_summarise_of_a_constant_series_has_zero_spread():
    out = summarise(np.full(100, 7.0))
    assert out["mean"] == pytest.approx(7.0)
    assert out["sd"] == pytest.approx(0.0)
    assert out["p10"] == pytest.approx(out["p90"])
```

- [ ] **Step 2: Run and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_compose.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'tt.models'`

- [ ] **Step 3: Implement**

```python
# analytics/src/tt/models/__init__.py
"""Projection models. Volume is predicted; efficiency and TD rate are regressed."""
```

```python
# analytics/src/tt/models/compose.py
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
```

- [ ] **Step 4: Run and watch it pass**

Run: `.venv/bin/python -m pytest tests/test_compose.py -q`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add analytics/src/tt/models analytics/tests/test_compose.py
git commit -m "feat(analytics): Monte Carlo points composition"
```

---

### Task 8: Baseline evaluation on real data

The moment of truth: does anything beat the trailing average?

**Files:**
- Create: `analytics/src/tt/studies/baselines.py`, `analytics/tests/test_baselines.py`

**Interfaces:**
- Produces: `compare_baselines(df, seasons, start_week=5, n_values=(3, 8)) -> DataFrame`.

- [ ] **Step 1: Write the failing test**

```python
# analytics/tests/test_baselines.py
import pandas as pd
import pytest
from tt.studies.baselines import compare_baselines


def synthetic():
    """A player whose points are a constant plus noise-free trend."""
    rows = []
    for player in ("A", "B", "C"):
        base = {"A": 5.0, "B": 10.0, "C": 15.0}[player]
        for week in range(1, 11):
            rows.append({"player_id": player, "season": 2025, "week": week,
                         "points": base})
    return pd.DataFrame(rows)


def test_compare_baselines_returns_a_row_per_baseline():
    out = compare_baselines(synthetic(), seasons=(2025,), start_week=5)
    assert set(out["baseline"]) >= {"last_3", "last_8"}


def test_a_trailing_mean_is_perfect_on_a_constant_series():
    # If the harness cannot report a perfect score on data where the baseline
    # IS the answer, the harness is broken, not the baseline.
    out = compare_baselines(synthetic(), seasons=(2025,), start_week=5)
    last3 = out.set_index("baseline").loc["last_3"]
    assert last3["mae"] == pytest.approx(0.0, abs=1e-9)
    assert last3["n"] > 0


def test_compare_baselines_reports_every_metric():
    out = compare_baselines(synthetic(), seasons=(2025,), start_week=5)
    assert {"baseline", "n", "mae", "rmse", "spearman"} <= set(out.columns)
```

- [ ] **Step 2: Run and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_baselines.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'tt.studies.baselines'`

- [ ] **Step 3: Implement**

```python
# analytics/src/tt/studies/baselines.py
"""Baseline comparison over real weeks.

Spec §4.3: a model must beat these to justify existing. Reporting them honestly
is the point -- if the trailing average wins, that is the finding.
"""
from __future__ import annotations

import pandas as pd

from ..backtest import baseline_last_n, evaluate, walk_forward


def compare_baselines(
    df: pd.DataFrame,
    seasons: tuple[int, ...],
    start_week: int = 5,
    n_values: tuple[int, ...] = (3, 8),
) -> pd.DataFrame:
    """Walk forward through every week, scoring each trailing-mean baseline."""
    subset = df[df["season"].isin(seasons)]
    results: dict[str, list[tuple[float, float]]] = {f"last_{n}": [] for n in n_values}

    for season, week in walk_forward(subset, min(seasons), start_week):
        actual = subset[(subset["season"] == season) & (subset["week"] == week)]
        if actual.empty:
            continue
        for n in n_values:
            pred = baseline_last_n(subset, season, week, n=n)
            merged = actual.merge(pred, on="player_id", how="inner")
            if merged.empty:
                continue
            results[f"last_{n}"].extend(
                zip(merged["pred"].tolist(), merged["points"].tolist())
            )

    rows = []
    for name, pairs in results.items():
        if not pairs:
            continue
        preds = pd.Series([p for p, _ in pairs]).to_numpy()
        actuals = pd.Series([a for _, a in pairs]).to_numpy()
        rows.append({"baseline": name, **evaluate(preds, actuals)})
    return pd.DataFrame(rows)
```

- [ ] **Step 4: Run and watch it pass**

Run: `.venv/bin/python -m pytest tests/test_baselines.py -q`
Expected: 3 passed.

- [ ] **Step 5: Run it on real data and record the numbers**

```bash
.venv/bin/python -c "
from pathlib import Path
from tt.ingest import load_seasons
from tt.scoring import score_frame
from tt.studies.baselines import compare_baselines

df = load_seasons('stats_player', [2024, 2025], Path('data'))
df = df[(df['season_type']=='REG') & df['position'].isin(['QB','RB','WR','TE'])]
df['points'] = score_frame(df)
print(compare_baselines(df, seasons=(2024, 2025), start_week=5).to_string(index=False))
"
```
Record the output in your report. These numbers are the bar every future model must clear.

- [ ] **Step 6: Commit**

```bash
git add analytics/src/tt/studies/baselines.py analytics/tests/test_baselines.py
git commit -m "feat(analytics): baseline comparison over real seasons"
```

---

### Task 9: Documentation

**Files:**
- Create: `analytics/README.md`
- Modify: `docs/prediction-engine-design.md` (record measured baselines and the DvP replication)

- [ ] **Step 1: Write `analytics/README.md`**

Cover: what the package is, how to create the venv with `uv`, how to run the tests, how to fetch data, and the one rule that matters — every feature is point-in-time and the leakage test is the guard.

- [ ] **Step 2: Record the real numbers in the spec**

Add the Task 6 DvP replication table and the Task 8 baseline table to `docs/prediction-engine-design.md`, resolving §9.1. If the multi-season replication contradicts §2.2, say so plainly and revise the opponent-adjustment guidance.

- [ ] **Step 3: Commit**

```bash
git add analytics/README.md docs/prediction-engine-design.md
git commit -m "docs(analytics): package README and measured baselines"
```

---

## Deferred to Phase 2

- The decision layer (spec §4.5): draft VOR + ADP survival, lineup optimiser, playoff variance targeting, FAAB bidding.
- A learned volume model (GBM) to challenge the trailing-average baselines.
- Vegas implied team totals (spec §9.2) — no clean free source identified.
- Node CLI integration (`tt project`).
