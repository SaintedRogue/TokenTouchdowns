# tt-analytics

Component-wise fantasy football projection pipeline. Historical outcomes come
from nflverse; predictions are distributions, not point estimates, built by
modelling the parts of performance that are actually predictable (volume) and
regressing the parts that are not (efficiency, TD rate). See
[`../docs/prediction-engine-design.md`](../docs/prediction-engine-design.md)
for the architecture and the empirical basis behind it.

## Setup

From this directory:

```sh
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
```

## Tests

```sh
.venv/bin/python -m pytest -q
```

77 tests currently pass.

## Fetching data

```python
from pathlib import Path
from tt.ingest import load_seasons

df = load_seasons("stats_player", range(2015, 2026), Path("data"))
```

Downloads are cached to `data/` (one parquet file per season, gitignored) and
reused on later calls; pass `force=True` to `fetch_season` to refetch a
season. Each cached season is roughly 800 KB (parquet, not the much larger
CSV nflverse also publishes), so a full 2015-2025 cache is roughly 8 MB total.

## The one rule that matters

Every feature is point-in-time. `tt.features.prior_weeks` is the only
sanctioned gateway to history: it returns rows strictly before the (season,
week) being predicted, and every other feature function must route through
it rather than filtering a DataFrame itself.

`prior_weeks` also self-checks — it raises `PointInTimeError` if it ever
returns a row it should not have. That guard only covers this module; it
cannot stop code elsewhere from filtering a DataFrame directly instead of
calling `prior_weeks`, so new feature code must route through it deliberately.

A leaky feature makes every downstream number optimistic, and it is invisible
without this guard: a backtest built on leaked data looks better than the
model actually is, and nothing about the output tells you so.
